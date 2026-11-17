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

### Legacy `.ozagia.toml` and `ozagia/config.toml`

The registry file is now also read by the runtime project loader
(`ozagia.config.load_project_config`), which supplies `ProjectConfig` for the
index, memory, adapters, reads, tests, lint, jobs, IaC, review, environment,
service, relay and runtime layers. It merges two sources, highest priority
first:

1. the legacy file: `$OZAGIA_CONFIG`, `<root>/.ozagia.toml` or
   `<state dir>/config.toml`;
2. `<root>/ozagia/config.toml`.

The legacy file wins for **every key it defines**, so an existing project keeps
its exact behaviour. The registry file supplies only the supported keys the
legacy file leaves unset, key by key.

`[environments]`, `[services]`, `[relays]` and `[runtime]` are project models,
not flat settings, so their precedence unit is the **named entry**:
`[environments.NAME]`, `[services.NAME]`, `[relays.NAME]`,
`[runtime.services.NAME]` and `[runtime.tunnels.NAME]`. A legacy entry wins as
a whole over a versioned entry with the same name; the versioned entry's fields
are ignored rather than merged into it. Versioned-only names are appended in
their file's order. Keys are never combined across files for one entry, so a
legacy relay chain cannot acquire a versioned `allowed_kinds`, and a legacy
runtime command cannot acquire a versioned relay. Entries in one file are
validated by the same parser as the legacy file: duplicate names, unknown
fields, unsafe argv, relay specs, ports and cross-references keep the same
messages and limits, and the error names the file that defines the entry.

`ozagia.config.config_path()` returns
the legacy file when present, else `ozagia/config.toml`;
`ozagia.config.config_sources()` reports both paths, which one is active
(`legacy` or `repo`), their precedence and the files that supplied the loaded
configuration. `ozagia.config.load_config_section(root, section)` returns one
named table merged the same way, with the source of every key and both paths;
it is uncached, so an edit is visible on the next call. `ozagia doctor`
reports the same information in its `config` section (JSON and the Python
API). The text summary names both paths, the active source, the precedence
and a bounded unmapped count; when the list is truncated it shows
`20 of 25…` without printing key names or values.

`$OZAGIA_CONFIG` may name either file. When it names the versioned file of the
same root it keeps the registry schema; when it names a config from another
root (an eval workdir or a clone), the loader accepts the registry's
`[testing]` table as the `[tests]` alias and reports the groups other modules
own, so the file still loads.

The project loader consumes these registry keys:

| Group | Keys |
| --- | --- |
| `index` | every key in the legacy `[index]` table |
| `memory` | `skip_dirs` |
| `adapters` | `modules` (a module from `ozagia/config.toml` imports from the repository root) |
| `reads` | `structured_outline_lines`, `structured_outline_bytes`, `outline_lines`, `outline_bytes`, `repeat`, `repeat_by_harness` |
| `tests` / `testing` | `select`/`cache`, and the `testing.captured`/`testing.cache` aliases |
| `jobs` | `enabled` |
| `lint` | `enabled`, `timeout_s`, `python`, `rust` (`auto` means unset) |
| `iac` | `roots`, `skip_globs` |
| `review` | `layers` |
| `environments` | one named table per environment; every legacy environment field |
| `services` | one named table per service; every legacy service field |
| `relays` | one named table per relay; `specs`, `allowed_kinds`, `max_timeout_s`, `command_allowlist`, `max_output_bytes` |
| `runtime.services` | one named table per service; `command`, `workdir`, `relay`, `mode`, `description` |
| `runtime.tunnels` | one named table per tunnel; `relay`, `target`, `local_port`, `description` |

Empty `index.map_dirs`, `index.import_roots`, `index.path_prefixes` and
`index.command_docs` mean "derive the project default", matching the registry
descriptions. `testing.captured` is reduced to the two selectors the captured
executor implements: `affected` and `smoke` mean `affected`; `none`, `off`,
`package` and `all` mean `all`.

`[security]` is consumed by `ozagia.core.security` from either file (legacy
wins per key) through `ozagia.config.load_config_section`; its supported keys
are not reported as unmapped. `[environments]`, `[services]`, `[relays]` and
`[runtime]` are consumed by `ProjectConfig` from either file with the named-entry
precedence above, so they are not reported as unmapped either; their fields
are validated by the legacy parser. Everything else stays with its own module:
`hints.*` is read by `ozagia.core.library_hints`, `models.*` by
`ozagia.core.models`, and the `ui`, `resources`, `worktrees`, `markdown`,
`files` and `tasks` groups by their modules. The project loader reports those
keys in `ProjectConfig.unmapped` (and doctor lists them) instead of silently
ignoring or copying them. The same applies to other legacy sections owned by
another module, such as `[hints]`: they are reported and left in place.
Unknown and nested `[security]` keys are reported the same way, while the
supported security keys are accepted from either file.

Invalid values in `ozagia/config.toml` are a `ConfigError` that names the file,
for example `/repo/ozagia/config.toml: source_dirs entry '../outside' must be
inside the root`. This includes a supported `[security]` key with the wrong
type. A malformed legacy file stays forgiving for `ozagia.core.security`
(defaults and no crash); see `docs/security.md`.

Example: keep the legacy graph scope and move the rest of the project config
into the versioned file.

```toml
# .ozagia.toml (still present; wins for every key it defines)
[index]
source_dirs = ["src"]
```

```toml
# ozagia/config.toml (supplies every supported key the legacy file omits)
[index]
test_dirs = ["tests", "spec"]
docs = ["docs/**/*.md"]

[memory]
skip_dirs = ["generated"]

[testing]
captured = "affected"
cache = true

[lint]
python = "ruff"

[jobs]
enabled = true

[security]
block_secrets = false
secret_allowlist = ["path:tests/fixtures/**"]

[iac]
roots = ["infra"]

[review]
layers = ["src/core", "src/api"]

[environments.dev]
paths = ["envs/dev"]
region = "us-east-1"

[services.api]
chart = "charts/api"
environment = "dev"

[relays.local]
specs = [{ kind = "local" }]

[runtime.services.api]
command = ["python", "-m", "app"]
workdir = "services/api"

[runtime.tunnels.database]
relay = "local"
target = "db.internal.example:5432"

[hints]
library = "strict"
```

`load_project_config()` uses `src` from the legacy file, `tests`/`spec`,
`docs`, the memory skip, testing, lint, jobs, IaC, review, environment, service,
relay and runtime values from the versioned file, and reports `hints` in
`unmapped` (the hints module reads it). The versioned `[environments.dev]`,
`[services.api]`, `[relays.local]`, `[runtime.services.api]` and
`[runtime.tunnels.database]` entries are added because the legacy file defines
none of those names. `ozagia.core.security.load_config()` reads `block_secrets`
and `secret_allowlist` from the versioned `[security]` table because the legacy
file does not define them. `config_path()` returns `.ozagia.toml`;
`config_sources()` reports both files with
`precedence == ("legacy", "repo")`.

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
| `index.map_dirs` | string_list | `[]` (derived) | repo | yes |
| `index.docs_all` | string_list | `["**/*.md"]` | repo | yes |
| `index.key_docs` | string_list | `["README.md"]` | repo | yes |
| `index.command_docs` | string_list | `[]` (uses key_docs) | repo | yes |
| `index.import_roots` | string_list | `[]` (derives `src`) | repo | yes |
| `index.path_prefixes` | string_list | `[]` (derived) | repo | yes |
| `index.changed_exclude` | string_list | `["__pycache__/", ".pytest_cache/", ".ozagia/"]` | repo | yes |
| `index.entrypoint_prefixes` | string_list | `["scripts/", "tools/", "bin/"]` | repo | yes |

Empty derived values have the same meaning as in `ozagia.config`: the
loader derives map directories, command docs, import roots and path prefixes
from the other index settings when they are not set explicitly.

### memory and adapters

| Key | Type | Default | Scope | Restart |
| --- | --- | --- | --- | --- |
| `memory.skip_dirs` | string_list | `[]` | repo | yes |
| `adapters.modules` | string_list | `[]` | repo | yes |

`adapters.modules` holds `module:attr` names. Reading the setting never
imports them; the existing adapter loader still imports only when a caller
asks it to. `ozagia.adapters.registry` reloads when either configuration file
changes (path, `mtime_ns`, size and content digest of the legacy file and
`ozagia/config.toml`, plus `$OZAGIA_ADAPTERS`), so editing the non-active file
takes effect on the next call. `reload=True` still forces a rebuild.

### reads

| Key | Type | Default | Scope | Restart |
| --- | --- | --- | --- | --- |
| `reads.structured_outline_lines` | integer | `2000` | repo | no |
| `reads.structured_outline_bytes` | integer | `65536` | repo | no |
| `reads.outline_lines` | integer | `400` | repo | no |
| `reads.outline_bytes` | integer | `40960` | repo | no |
| `reads.repeat` | string | `"note"` | repo | no |
| `reads.repeat_by_harness.<harness>` | string | none | repo | no |

`reads.repeat` and each per-harness entry are `stub`, `note` or `off`. The
per-harness table is dynamic: every harness present is reported as its own
setting, for example `reads.repeat_by_harness.opencode = "stub"`.

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
| `models.tasks.<class>` | string | none | both | no |
| `models.local.providers` | string_list | `[]` | both | no |
| `models.local.models` | string_list | `[]` | both | no |

An empty model id means "use the host default" (`default`) or "no fallback"
(`fallback`). Model ids are bounded `provider/model`-style strings.

`[models.tasks]` and `[models.local]` are dynamic tables: every task class or
local list that is present becomes its own setting in `values()` and can be
read or previewed with `get`/`set`. They have no built-in default and therefore
do not appear in `schema`. `[models.by_task_class]` is accepted as an alias for
`[models.tasks]`; defining both in one file is an error. Repository entries
override user entries for the same task class; local providers and models are
unioned across layers. `ozagia.core.models` reads the same tables through its
own loader; `settings.model_overrides(root)` returns its effective
`tasks`/`local` maps for callers that need the registry's view.

`[model_graph]` is a separate direct-read overlay, not a registry section. It
holds optional model-profile, family, task-profile and distance-weight
overrides for the advisory graph in `ozagia.core.model_graph`. Unknown keys are
reported in the graph report rather than rejected. See
[model_graph.md](model_graph.md) for the schema and precedence.

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
| `review.layers` | string_list | `[]` | repo | yes |

### lint

| Key | Type | Default | Scope | Restart |
| --- | --- | --- | --- | --- |
| `lint.enabled` | boolean | `true` | repo | no |
| `lint.timeout_s` | number | `3.0` | repo | no |
| `lint.python` | string | `"auto"` | repo | no |
| `lint.rust` | string | `"auto"` | repo | no |
| `lint.trust_project_venv` | boolean | `false` | user | no |

`lint.python` is `auto`, `ruff` or `off`; `lint.rust` is `auto`, `rustfmt`
or `off`. The legacy loader treats an unset `enabled` as automatic; the
registry default is `true`. `lint.trust_project_venv` is user-only and is
read by the linter from the user config, not the repository config.

### environments

| key | type | default | scope | restart |
|---|---|---|---|---|
| `environments.trust_project_python` | boolean | `false` | user | no |

User-only: whether `ozagia env`, `dev_environment` and the repo review may
run interpreters the project ships to probe them
([environments.md](environments.md#what-is-run)). `OZAGIA_TRUST_PROJECT_PYTHON=1`
does the same for one process. The `[environments]` profile tables of a
legacy `.ozagia.toml` are unrelated.

### jobs

| Key | Type | Default | Scope | Restart |
| --- | --- | --- | --- | --- |
| `jobs.enabled` | boolean | `false` | repo | no |

Background jobs remain opt-in per repository. Enabling the setting does not
start anything.

### security

| Key | Type | Default | Scope | Restart |
| --- | --- | --- | --- | --- |
| `security.enabled` | boolean | `true` | repo | no |
| `security.tools` | string_list | `[]` | repo | no |
| `security.allow_network` | boolean | `false` | repo | no |
| `security.fail_on` | string | `"high"` | repo | no |
| `security.exclude` | string_list | `["tests/**", "test/**", "docs/**", "vendor/**"]` | repo | no |
| `security.max_findings` | integer | `200` | repo | no |
| `security.block_secrets` | boolean | `true` | repo | no |
| `security.secret_allowlist` | string_list | `[]` | repo | no |

`security.tools` is a subset of the fixed offline tool registry; nothing is
installed or run by setting it. `security.fail_on` is `blocking`, `high`,
`medium`, `low` or `never`. `security.secret_allowlist` accepts `path:`,
`rule:` and `sha256:` entries and never contains the secret value.

`ozagia.core.security` reads `[security]` from both files with
`ozagia.config.load_config_section`: the legacy `.ozagia.toml`/state/env file
wins for every key it defines and `ozagia/config.toml` fills the rest.
`load_config()` returns the effective values plus `sources` (per key), `files`
(both paths), and `error`/`errors` (`legacy_error` and `repo_error` name the
offending file). A malformed legacy file stays forgiving: its values are
skipped and the defaults or the valid versioned values apply. A malformed or
invalid versioned file is reported instead of silently ignored, and
`load_config_strict()` raises `ConfigError`; the normal scan still completes
and includes the error in `config.error`, `config.errors` and its warnings.
`max_findings` is capped at 1000 by the security loader even though the
registry accepts up to 10000.

### iac

| Key | Type | Default | Scope | Restart |
| --- | --- | --- | --- | --- |
| `iac.roots` | string_list | `[]` | repo | yes |
| `iac.skip_globs` | string_list | `[]` | repo | yes |

The built-in backup-file globs still apply. Legacy `iac.remote_states` is an
array of tables and is reported, not migrated, until it has a registry shape.

### testing

| Key | Type | Default | Scope | Restart |
| --- | --- | --- | --- | --- |
| `testing.mode` | string | `"affected"` | both | no |
| `testing.fallback` | string | `"all"` | both | no |
| `testing.max_tests` | integer | `50` | both | no |
| `testing.timeout_s` | integer | `300` | both | no |
| `testing.captured` | string | `"all"` | both | no |
| `testing.cache` | boolean | `false` | both | no |

`testing.mode` and `testing.captured` are one of `none`, `smoke`, `affected`,
`package` or `all`. `testing.captured` also accepts `off` as an alias for
`none`. `[tests] select` and `[tests] cache` are accepted as aliases for
`testing.captured` and `testing.cache`, so migrated legacy files keep working
in place. `testing.fallback` is one of `all`, `refuse` or
`affected_with_warning`. `testing.mode` is the default for bounded tasks;
`testing.captured` is the interactive captured-pytest default and stays `all`
unless a repository opts into a subset. An explicit `[tests] select` in
`.ozagia.toml` still wins over `testing.captured`; see `docs/testing.md` for
the precedence and the executor mapping. `testing.timeout_s` is recorded for a
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

[reads]
structured_outline_lines = 2000
outline_lines = 400
repeat = "note"

[reads.repeat_by_harness]
opencode = "stub"

[lint]
enabled = true
timeout_s = 3
python = "auto"
rust = "auto"

[jobs]
enabled = false

[security]
enabled = true
tools = []
allow_network = false
fail_on = "high"
exclude = ["tests/**", "vendor/**", "docs/**"]
max_findings = 200
block_secrets = true
secret_allowlist = []

[models]
default = ""
fallback = ""
local_only = false

[models.tasks]
diff_review = "openai/gpt-5-mini"

[models.local]
providers = ["ollama", "lmstudio"]
models = ["vllm/private"]

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

`ozagia config` delegates to `ozagia.settings_cli`; the module entry point
and the `ozagia-settings` console script remain available:

```text
ozagia config schema [--json]
ozagia config get KEY [--root R] [--json]
ozagia config values [--root R] [--json]
ozagia config set KEY VALUE --scope repo|user [--root R] [--dry-run] [--json]
ozagia config migrate [--root R] [--dry-run|--apply --confirm TOKEN] [--json]
```

Examples:

```bash
# The fixed registry: defaults, scopes, validations and aliases.
ozagia config schema --json

# Every effective value and its source, including [models.tasks] entries.
ozagia config values --root .

# One setting, with its source and default.
ozagia config get resources.job_memory_mb --root .

# Preview a user preference; nothing is written.
ozagia config set ui.theme dark --scope user --dry-run

# Apply a repository-only index setting; lists are comma-separated or JSON.
ozagia config set index.test_dirs tests,spec --root .

# Preview a resource limit without creating ozagia/.
ozagia config set resources.job_pids 512 --root . --dry-run

# Preview a per-task model pin and a local-provider list.
ozagia config set models.tasks.diff_review openai/gpt-5-mini --root . --dry-run
ozagia config set models.local.providers ollama,lmstudio --root . --dry-run

# Preview the legacy .ozagia.toml migration; nothing is written.
ozagia config migrate --root .

# Apply the exact preview with the token it printed.
ozagia config migrate --root . --apply --confirm TOKEN
```

Exit status is `0` on success and `2` for a validation, configuration or IO
error. `config set` applies unless `--dry-run` is passed; the Python API
`set_value()` defaults to `dry_run=True`. `config migrate` previews unless
`--apply` is passed.

## Migrating `.ozagia.toml`

`ozagia config migrate` copies legacy settings into `ozagia/config.toml`
through the registry's validation. It is preview-first:

1. The default is a dry run. It returns a unified diff, a per-key report and
   a confirmation token.
2. `--apply --confirm TOKEN` writes the previewed values in one atomic
   update. The token covers the legacy and target file contents and the exact
   planned values; a changed file or a wrong token is refused as stale.
3. Re-running after an apply reports `same` for values already present and
   makes no write. A no-op apply needs no token.

The legacy file is **never deleted, rewritten or moved**. The migration only
adds or updates `ozagia/config.toml`, so existing loaders keep working and
`ozagia.core.settings` sees the same values. The mapping is:

| Legacy | Registry | Notes |
| --- | --- | --- |
| `[index]` | `index.*` | all legacy index keys |
| `[memory] skip_dirs` | `memory.skip_dirs` | |
| `[adapters] modules` | `adapters.modules` | |
| `[reads]` | `reads.*` | `repeat_by_harness` becomes one setting per harness |
| `[tests] select` | `testing.captured` | `all`/`affected` are the same modes |
| `[tests] cache` | `testing.cache` | |
| `[jobs] enabled` | `jobs.enabled` | |
| `[lint]` | `lint.*` | `trust_project_venv` is user-only and reported, not copied |
| `[security]` | `security.*` | |
| `[hints]` | `hints.*` | |
| `[review] layers` | `review.layers` | |
| `[iac] roots`, `skip_globs` | `iac.*` | `remote_states` is reported, not copied |

Unsupported keys are reported, never guessed:

- `unknown`: not a legacy key or not a known section; a close suggestion is
  included when one exists.
- `unmapped`: a known legacy setting with no registry equivalent yet, such
  as `iac.remote_states` or `lint.trust_project_venv`. The project models
  `[environments]`, `[services]`, `[relays]` and `[runtime]` are reported
  here too because the settings registry has no typed keys for them, even
  though `load_project_config` now reads them from either file. They stay in
  `.ozagia.toml` unless copied by hand.
- `conflict`: `ozagia/config.toml` already has a different value. The
  existing value is kept and named in the report; resolve it with
  `ozagia config set` and preview again.
- `invalid`: the value violates the registry; it is not written and the
  command exits 1.

After an apply, `ozagia.config.load_project_config` reads the new values
immediately. The legacy file still wins for every key it defines, so the
migrated repository behaves as before; values the legacy file does not define
now come from `ozagia/config.toml`. Removing the legacy file is a separate
step and is safe only after checking the migration report:

- `[environments]`, `[services]`, `[relays]` and `[runtime]` are read from
  either file by `ozagia.config.load_project_config`, with legacy entries
  winning by name. `config migrate` does not copy them because the settings
  registry has no typed keys for them; copy those tables by hand if the legacy
  file is going away.
- `ozagia.core.security` reads `[security]` from both files with legacy
  precedence, so the migrated `[security]` table keeps working after the legacy
  file is removed.
- Any `unmapped` or `unknown` entry stays in `.ozagia.toml` by design.

When both files define a supported key, the legacy value wins and the
registry value is ignored for the runtime configuration (the `settings`
commands still report the registry's own value). This is deliberate
backward compatibility during the transition.

Example JSON shape:

```json
{"schema": 1, "root": "/repo", "legacy": "/repo/.ozagia.toml",
 "target": "/repo/ozagia/config.toml", "dry_run": true, "ok": true,
 "changed": true, "applied": false, "token": "...",
 "entries": [{"legacy": "tests.select", "key": "testing.captured",
              "status": "move", "value": "affected"}],
 "counts": {"move": 1}, "conflicts": 0, "unmapped": 0, "unknown": 0,
 "diff": "--- ..."}
```

The migration is not a replacement for the existing loaders, but the project
loader now reads the migrated file: `ozagia.config` merges
`ozagia/config.toml` under `.ozagia.toml` for the graph, index, memory,
adapters, reads, tests, lint, jobs, IaC, review, environments, services,
relays and runtime, and `ozagia.core.security` reads `[security]` from both
with legacy precedence. The legacy file is kept and keeps priority so
behaviour does not change during the transition; hints and models still need
their existing files and loaders, and the project-model tables must be copied
by hand if the legacy file is removed.

## JSON shapes

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

This module is deliberately standalone for the settings groups that have not
moved yet:

- `.ozagia.toml` and `ozagia/config.toml` are both read by
  `ozagia.config.load_project_config` for the index, memory, adapters, reads,
  jobs, tests, lint, IaC and review layers, and for the environments, services,
  relays and runtime project models (merged by named entry, legacy entries
  winning whole), and by `ozagia.core.security.load_config` for `[security]`.
  The legacy file keeps priority for every key it defines; the registry file
  supplies the remaining supported keys. `ozagia config migrate` copies the
  registry subset into `ozagia/config.toml` and keeps the legacy file; the
  project-model tables are not part of the registry subset.
- `ozagia.core.resources` still reads `ozagia/resources.toml` or `[resources]`
  from `ozagia/config.toml`. The canonical registry locations match its nested
  `limits`, `admission` and `worktrees` tables.
- `ozagia.core.files` still uses its built-in cleanup policy, and
  `ozagia.core.worktree` still reads `ozagia/worktrees.toml` and
  `ozagia/policies.toml`. `ozagia/worktrees.toml` (expected worktree/branch
  mapping) is separate from the registry's `worktrees` group (resource
  placement policy), which is stored under `[resources.worktrees]`.
- `ozagia.core.testing` reads the `testing` group and its open-ended tables.
  The interactive captured-pytest selector in `ozagia.intent.affected` reads
  `[tests] select`/`[tests] cache` from `.ozagia.toml` first; when `select` is
  not set it uses `testing.captured`. `ozagia.config` maps the same keys (and
  `testing.cache`) into `ProjectConfig`, keeping the legacy value when both
  files define one. `OZAGIA_TESTS_ALL=1` still runs the command as typed.
- `ozagia.core.models` reads `[models]` directly, including the same
  `[models.tasks]`/`[models.by_task_class]` and `[models.local]` tables the
  registry now validates. `settings.model_overrides(root)` returns the same
  effective task and locality maps for other callers.
- `ozagia.core.library_hints` reads the `hints` group on each handled edit and
  write (with `$OZAGIA_LIBRARY_HINTS=0` as a hard local opt-out); the daemon
  sums its `meta` counters.
- `ozagia.core.security` reads `[security]` from both `.ozagia.toml` and
  `ozagia/config.toml` through `ozagia.config.load_config_section`, with the
  legacy file winning per key. A malformed legacy file is forgiving; an
  invalid versioned file is reported (`load_config`) or raised
  (`load_config_strict`), so the commit/push gate and scans never silently
  ignore a broken versioned `[security]` table.
- `ozagia doctor` reports a `settings` section: the active user/repository/
  state paths, registry validity and the first error, per-source counts, and a
  bounded list of non-default keys. Its `config` section reports the legacy
  and versioned project-config paths, the active source and precedence, and the
  first 20 versioned-config keys the project loader did not map. The text
  `Config:` line names both paths, the active source, precedence and a bounded
  unmapped count (`20 of 25…` when truncated). A broken settings file is
  reported there and does not change doctor's exit status, which still follows
  the root and project-config validity.
- `ozagia config migrate` handles the legacy `.ozagia.toml` file. It is
  preview-first (dry run by default), applies only with the preview token,
  never deletes the legacy file and reports unknown, unmapped, conflicting
  and invalid keys. It does not install Git hooks; `ozagia hooks` does that
  separately and only when asked.

## Tests

```bash
PYTHONPATH=src python -m pytest -q tests/test_config.py \
  tests/test_settings.py tests/test_models.py tests/test_testing.py \
  tests/test_doctor.py tests/test_security.py tests/test_adapters.py \
  tests/test_affected_tests.py tests/test_cli.py tests/test_hooks.py
ruff check src tests
```
