# Settings registry and repository configuration

`ozagia.core.settings` is one typed registry for every setting. Each entry
reports its key, type, default, current value, source, scope, validation,
description and whether a restart (or reindex) is required. Configuration is
read from a user file and a versioned repository directory; writes are
validated, preview-first and atomic.

The registry is standard-library only. It makes no network or model calls and
does not import the target project.

## Locations

| Layer | Location | Notes |
| --- | --- | --- |
| built-in | this module | Always present; values are code defaults. |
| user | `$OZAGIA_USER_CONFIG` or `~/.config/ozagia/config.toml` | User-wide preferences. |
| repository | `<root>/ozagia/config.toml` | Versioned project configuration. |
| environment | explicit map below | A process environment override. |
| explicit | API `overrides=` argument | Highest precedence; not a file. |

`OZAGIA_ROOT` selects the target root (as in `ozagia.config`).
`OZAGIA_STATE_DIR` selects the state directory (absolute, or relative to the
root). Neither is a registry value; they change *where* ozagia works.

`ozagia/` (no leading dot) is versioned project configuration. It holds data
files only: it must not contain `__init__.py` and is never imported as a Python
package. The no-dot name is deliberate: it cannot collide with `.ozagia/` on
case-insensitive filesystems (macOS APFS, Windows), and it reads as project
configuration rather than local state. `.ozagia/` (leading dot) is ignored
local state: the graph snapshot, journal, jobs, resource history, edit backups
and file ledger live there. Add `.ozagia/` to the target's `.gitignore`. No
registry read creates either directory; an applied write creates only
`<root>/ozagia/config.toml`.

The wider `ozagia/` layout (from the UI/agent plan) is:

```text
ozagia/
  config.toml           # overrides; the registry schema
  apertures/*.toml      # view definitions
  tasks/*.toml          # bounded task classes
  models.toml
  prompts/*.md
  review.toml
  worktrees.toml        # expected worktree/branch mapping
  policies.toml         # commit/merge/push policy
  markdown.toml
  files.toml
  adapters.toml
  modules/              # opt-in local modules, never auto-imported
```

This slice owns `config.toml`. The other files in the layout are not read by
this slice yet; they keep their existing loaders where one exists.

## Precedence

Lowest to highest:

```text
built-in default < user config < repository config < environment < override
```

Every value carries its source: `default`, `user`, `repo`, `env` or
`override`. A layer may set only the scopes it is allowed to:

- `user`: the user config;
- `repo`: the repository config;
- `both`: either.

A repository-only setting in the user config is an error, and
`set --scope user` refuses it. Scope is enforced on reads and writes.
Environment values and explicit overrides are process-local and are not
restricted by file scope.

### Environment overrides

| Variable | Setting |
| --- | --- |
| `OZAGIA_RESOURCES_ENABLED` | `resources.enabled` |
| `OZAGIA_WORKTREES_ROOT` | `worktrees.root` |
| `OZAGIA_WORKTREES_AVOID_TMPFS` | `worktrees.avoid_tmpfs` |
| `OZAGIA_MARKDOWN_HISTORY` | `markdown.history` |
| `OZAGIA_MODELS_DEFAULT` | `models.default` |
| `OZAGIA_MODELS_LOCAL_ONLY` | `models.local_only` |
| `OZAGIA_TASKS_MAX_WALL_S` | `tasks.max_wall_s` |
| `OZAGIA_TESTING_MODE` | `testing.mode` |
| `OZAGIA_TESTING_FALLBACK` | `testing.fallback` |
| `OZAGIA_TESTING_MAX_TESTS` | `testing.max_tests` |
| `OZAGIA_TESTING_TIMEOUT_S` | `testing.timeout_s` |
| `OZAGIA_TESTING_CAPTURED` | `testing.captured` |
| `OZAGIA_HINTS_LIBRARY` | `hints.library` |
| `OZAGIA_HINTS_MAX_PER_EDIT` | `hints.max_per_edit` |
| `OZAGIA_HINTS_RECORD` | `hints.record` |
| `OZAGIA_HINTS_ECOSYSTEMS` | `hints.ecosystems` |
| `OZAGIA_UI_THEME` | `ui.theme` |

Environment values are parsed with the setting's type. An invalid value is an
error that names the variable, for example
`$OZAGIA_RESOURCES_ENABLED (resources.enabled): must be true or false`.
Booleans accept `true/false`, `1/0`, `yes/no` and `on/off`; integers and
numbers are decimal. `OZAGIA_MODELS_DEFAULT=` (empty) means "use the host
default".

## Registry groups

Default values are shown as JSON. `restart` means the daemon/reindex must run
again before the value takes effect.

### index

| Key | Type | Default | Scope | Restart |
| --- | --- | --- | --- | --- |
| `index.source_dirs` | string_list | `["."]` | repo | yes |
| `index.test_dirs` | string_list | `["tests"]` | repo | yes |
| `index.docs` | string_list | `["README.md", "docs/**/*.md"]` | repo | yes |
| `index.skip_dirs` | string_list | `[]` | repo | yes |

### resources

| Key | Type | Default | Scope | Restart |
| --- | --- | --- | --- | --- |
| `resources.enabled` | boolean | `true` | both | no |
| `resources.sample_s` | number | `2.0` | both | no |
| `resources.watchdog` | string | `"auto"` | both | no |
| `resources.history` | boolean | `true` | both | no |
| `resources.reserve_memory_pct` | number | `10.0` | both | no |
| `resources.reserve_disk_pct` | number | `5.0` | both | no |
| `resources.reserve_disk_gb` | number | `1.0` | both | no |
| `resources.min_free_memory_pct` | number | `15.0` | both | no |
| `resources.min_free_disk_pct` | number | `10.0` | both | no |
| `resources.min_free_disk_gb` | number | `2.0` | both | no |
| `resources.wait_max_s` | integer | `300` | both | no |
| `resources.job_memory_mb` | integer | `2048` | both | no |
| `resources.job_disk_mb` | integer | `4096` | both | no |
| `resources.job_pids` | integer | `256` | both | no |

### worktrees

| Key | Type | Default | Scope | Restart |
| --- | --- | --- | --- | --- |
| `worktrees.root` | string | `"../ozagia-wt"` | repo | no |
| `worktrees.min_free_gb` | number | `5.0` | both | no |
| `worktrees.avoid_tmpfs` | boolean | `true` | both | no |

### markdown

| Key | Type | Default | Scope | Restart |
| --- | --- | --- | --- | --- |
| `markdown.history` | string | `"history"` | repo | no |
| `markdown.history_index` | boolean | `true` | repo | no |
| `markdown.new_file_budget` | integer | `3` | both | no |
| `markdown.duplicate_threshold` | number | `0.85` | both | no |
| `markdown.root_allow` | string_list | `["README.md", "CHANGELOG.md", "LICENSE", "CONTRIBUTING.md", "AGENTS.md", "SECURITY.md"]` | repo | no |

### files

| Key | Type | Default | Scope | Restart |
| --- | --- | --- | --- | --- |
| `files.ledger` | boolean | `true` | both | no |
| `files.trash` | string | `".ozagia/trash"` | repo | no |
| `files.retention_days` | integer | `30` | both | no |

### models

| Key | Type | Default | Scope | Restart |
| --- | --- | --- | --- | --- |
| `models.default` | string | `""` | both | no |
| `models.fallback` | string | `""` | both | no |
| `models.local_only` | boolean | `false` | both | no |

An empty model id means "use the host default" (`default`) or "no fallback"
(`fallback`). Model ids are bounded `provider/model`-style strings.

### tasks

| Key | Type | Default | Scope | Restart |
| --- | --- | --- | --- | --- |
| `tasks.default_budget_tokens` | integer | `40000` | both | no |
| `tasks.max_wall_s` | integer | `900` | both | no |

### review

| Key | Type | Default | Scope | Restart |
| --- | --- | --- | --- | --- |
| `review.min_reviewers` | integer | `1` | both | no |
| `review.require_clean` | boolean | `true` | both | no |

### testing

| Key | Type | Default | Scope | Restart |
| --- | --- | --- | --- | --- |
| `testing.mode` | string | `"affected"` | both | no |
| `testing.fallback` | string | `"all"` | both | no |
| `testing.max_tests` | integer | `50` | both | no |
| `testing.timeout_s` | integer | `300` | both | no |
| `testing.captured` | string | `"all"` | both | no |

`testing.mode` and `testing.captured` are one of `none`, `smoke`, `affected`,
`package` or `all`. `testing.fallback` is one of `all`, `refuse` or
`affected_with_warning`. `testing.mode` is the default for bounded tasks;
`testing.captured` is the interactive captured-pytest default and stays `all`
unless a repository opts into a subset. `testing.timeout_s` is recorded for a
caller that executes the plan; the planner never runs tests.

### hints

| Key | Type | Default | Scope | Restart |
| --- | --- | --- | --- | --- |
| `hints.library` | string | `"gentle"` | both | no |
| `hints.max_per_edit` | integer | `2` | both | no |
| `hints.record` | boolean | `true` | both | no |
| `hints.ecosystems` | string_list | `["python", "javascript", "rust"]` | both | no |

`hints.library` is `off`, `gentle` (new/changed symbols only) or `strict`
(also existing symbols in explicitly edited files). `hints.max_per_edit` is
the per-file cap (0-20). `hints.ecosystems` accepts `python`, `javascript`
(including TypeScript) and `rust`; an empty list disables detection.
`$OZAGIA_LIBRARY_HINTS=0` is a hard local opt-out
and is not a registry setting. See `docs/library_hints.md`.

Two open-ended tables hold per-task and per-path overrides:

```toml
[testing.tasks]
code_review = "none"
diff_review = "affected"

[testing.paths]
"docs/**" = "none"
"src/**" = "affected"
```

The tables are validated by the same registry parser, report their source and
are available through `settings.testing_overrides(root)`. `values()` also
lists every present entry as `testing.tasks.<class>` or
`testing.paths.<glob>`, and `set` accepts those keys. A path key containing
`*`, `/` or other non-bare characters is written quoted. The test-policy
precedence (task specification, task class, path, repository, default) is
documented in `docs/testing.md`.

### ui

| Key | Type | Default | Scope | Restart |
| --- | --- | --- | --- | --- |
| `ui.default_aperture` | string | `"architecture"` | both | no |
| `ui.theme` | string | `"auto"` | both | no |

`ui.theme` is one of `auto`, `light`, `dark`. `ui.default_aperture` is a
lowercase aperture id.

## TOML layout

The canonical layout uses the same names as the registry keys. The resource
and worktree groups use the nested tables the existing resource policy reads:

```toml
[index]
source_dirs = ["."]
test_dirs = ["tests"]
docs = ["README.md", "docs/**/*.md"]
skip_dirs = ["vendor"]

[resources]
enabled = true
reserve_memory_pct = 10
reserve_disk_pct = 5
reserve_disk_gb = 1

[resources.limits]
job_memory_mb = 2048
job_disk_mb = 4096
job_pids = 256

[resources.admission]
min_free_memory_pct = 15
min_free_disk_pct = 10
min_free_disk_gb = 2
wait_max_s = 300

[resources.worktrees]
root = "../ozagia-wt"
min_free_gb = 5
avoid_tmpfs = true

[markdown]
history = "history"
new_file_budget = 3
duplicate_threshold = 0.85

[files]
trash = ".ozagia/trash"
retention_days = 30

[models]
default = ""
fallback = ""
local_only = false

[tasks]
default_budget_tokens = 40000
max_wall_s = 900

[review]
min_reviewers = 1
require_clean = true

[testing]
mode = "affected"
fallback = "all"
max_tests = 50
timeout_s = 300
captured = "all"

[testing.tasks]
code_review = "none"

[testing.paths]
"docs/**" = "none"

[hints]
library = "gentle"
max_per_edit = 2
record = true
ecosystems = ["python", "javascript", "rust"]

[ui]
default_aperture = "architecture"
theme = "auto"
```

For compatibility, these flat forms are also accepted when reading:

```toml
[resources]
job_memory_mb = 2048
min_free_memory_pct = 15

[worktrees]
root = "../ozagia-wt"
```

The same flat alias exists for the other `limits`/`admission` keys and for
`worktrees.min_free_gb` and `worktrees.avoid_tmpfs`. A write updates the form
already in the file. If the setting is not present, it writes the canonical
nested table. That keeps existing files stable and avoids duplicate
definitions.

## Commands

Use the module entry point. It stands in for the planned `ozagia config`
command until `cli.py` is wired, and no console script is installed yet:

```text
python -m ozagia.settings_cli schema [--json]
python -m ozagia.settings_cli get KEY [--root R] [--json]
python -m ozagia.settings_cli values [--root R] [--json]
python -m ozagia.settings_cli set KEY VALUE --scope repo|user [--root R] [--dry-run] [--json]
```

Examples:

```bash
# The registry, including defaults, scopes, validations and aliases.
python -m ozagia.settings_cli schema --json

# Every effective value and its source.
python -m ozagia.settings_cli values --root .

# One setting, with its source and default.
python -m ozagia.settings_cli get resources.job_memory_mb --root .

# Preview a user preference; nothing is written.
python -m ozagia.settings_cli set ui.theme dark --scope user --dry-run

# Apply a repository-only index setting; lists are comma-separated or JSON.
python -m ozagia.settings_cli set index.test_dirs tests,spec --root .

# Preview a resource limit without creating ozagia/.
python -m ozagia.settings_cli set resources.job_pids 512 --root . --dry-run
```

Exit status is `0` on success and `2` for a validation, configuration or IO
error. The CLI applies unless `--dry-run` is passed; the Python API
`set_value()` defaults to `dry_run=True`.

JSON shapes:

```json
{"schema": 1, "settings": [{"key": "ui.theme", "...": "..."}]}
{"schema": 1, "root": "/repo", "locations": {"repo_config": "/repo/ozagia/config.toml"},
 "settings": {"ui.theme": {"value": "auto", "source": "default"}}}
{"schema": 1, "root": "/repo", "setting": {"key": "ui.theme", "value": "auto"}}
{"schema": 1, "key": "ui.theme", "path": "/home/u/.config/ozagia/config.toml",
 "dry_run": true, "changed": true, "applied": false, "diff": "--- ..."}
```

## Validation and writes

- Types are checked against the registry: `boolean`, `integer`, `number`,
  `string` or `string_list`. Ranges and choices are part of the entry.
- Unknown keys and sections are rejected with the file path, the dotted key and
  a close suggestion, for example:
  `.../ozagia/config.toml: unknown setting 'index.source_dir'; did you mean
  'index.source_dirs'?`
- A preview returns a unified diff and changes nothing. It does not create
  `ozagia/` or `.ozagia/`.
- An applied write creates parent directories, writes a temporary file in the
  same directory, `fsync`s it, preserves the existing file mode and replaces
  the target with `os.replace`. A concurrent edit is detected before the
  replace and refused. Unrelated TOML, comments and formatting are preserved;
  a comment on the target assignment's line is kept when the value is
  single-line, and only the target assignment (or a new section at the end)
  changes.
- `ozagia/config.toml` is versioned; use git for its history. The writer does
  not create a state-dir backup for it. This is separate from `edit/txn.py`,
  which edits target code and writes a backup under `.ozagia/`: a settings
  write may target the user config outside the repository, and the versioned
  config's history is git.

## Integration status

This module is deliberately standalone until the existing loaders are wired:

- `.ozagia.toml` still loads through `ozagia.config` for the index, memory,
  reads, jobs, tests and lint. The registry's `index` group mirrors the subset
  that will migrate; it does not replace that parser yet.
- `ozagia.core.resources` still reads `ozagia/resources.toml` or `[resources]`
  from `ozagia/config.toml`. The canonical registry locations match its nested
  `limits`, `admission` and `worktrees` tables.
- `ozagia.core.files` still uses its built-in cleanup policy, and
  `ozagia.core.worktree` still reads `ozagia/worktrees.toml` and
  `ozagia/policies.toml`. `ozagia/worktrees.toml` (expected worktree/branch
  mapping) is separate from the registry's `worktrees` group (resource
  placement policy), which is stored under `[resources.worktrees]`.
- `ozagia.core.testing` reads the `testing` group and its open-ended tables.
  The interactive captured-pytest selector in `ozagia.intent.affected` still
  reads `[tests] select` and `[tests] cache` from `.ozagia.toml`; this slice
  records `testing.captured` but does not change the interactive default.
- `ozagia.core.library_hints` reads the `hints` group on each handled edit and
  write (with `$OZAGIA_LIBRARY_HINTS=0` as a hard local opt-out); the daemon
  sums its `meta` counters.
- `ozagia doctor` does not yet report registry sources.
- There is no migration command from `.ozagia.toml` to `ozagia/config.toml` in
  this slice.

## Tests

```bash
PYTHONPATH=src python -m pytest -q tests/test_settings.py
ruff check src tests
```
