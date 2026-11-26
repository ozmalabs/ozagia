# Configuration

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/core/settings.py`, `src/ozagia/config.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

Two layers: the typed **settings registry** (read with `ozagia config schema`, written with `ozagia config set`) and the **project file** (`.ozagia.toml` at the repository root, resolved into `ozagia.config.ProjectConfig`).

## Settings registry (84 keys)

### `index`

| key | type | default | scope | env | validation | description |
|---|---|---|---|---|---|---|
| `index.source_dirs` | string_list | ["."] | repo |  | one or more repository-relative directories | Directories indexed into the graph; repository-relative. |
| `index.test_dirs` | string_list | ["tests"] | repo |  | repository-relative directories (may be empty) | Directories whose test files become Test nodes. |
| `index.docs` | string_list | ["README.md", "docs/**/*.md"] | repo |  | repository-relative glob patterns (may be empty) | Documentation globs ingested as Doc nodes. |
| `index.skip_dirs` | string_list | [] | repo |  | single directory names (may be empty) | Extra directory names skipped by the indexers. |
| `index.map_dirs` | string_list | [] | repo |  | repository-relative directories (may be empty) | Directories scanned by the repo map, search and symbol graph; empty derives source_dirs plus test_dirs. |
| `index.docs_all` | string_list | ["**/*.md"] | repo |  | repository-relative glob patterns (may be empty) | Additional documentation globs used by include_docs="all". |
| `index.key_docs` | string_list | ["README.md"] | repo |  | repository-relative glob patterns (may be empty) | Documentation listed first in the repository brief. |
| `index.command_docs` | string_list | [] | repo |  | repository-relative glob patterns (may be empty) | Documentation scanned for build and test commands; empty uses key_docs. |
| `index.import_roots` | string_list | [] | repo |  | repository-relative directories (may be empty) | Additional Python import roots; empty derives src when it exists. |
| `index.path_prefixes` | string_list | [] | repo |  | single path components (may be empty) | First path components recognised as file mentions; empty derives them from source_dirs, test_dirs and docs. |
| `index.changed_exclude` | string_list | ["__pycache__/", ".pytest_cache/", ".ozagia/"] | repo |  | repository-relative path prefixes or globs | Path prefixes hidden from the changed-file list. |
| `index.entrypoint_prefixes` | string_list | ["scripts/", "tools/", "bin/"] | repo |  | repository-relative path prefixes or globs | Path prefixes that are never reported as dead code. |

### `memory`

| key | type | default | scope | env | validation | description |
|---|---|---|---|---|---|---|
| `memory.skip_dirs` | string_list | [] | repo |  | single directory names (may be empty) | Additional directory names skipped by the graph build only. |

### `adapters`

| key | type | default | scope | env | validation | description |
|---|---|---|---|---|---|---|
| `adapters.modules` | string_list | [] | repo |  | module:attr adapter names (may be empty) | Opt-in project adapter modules as "module:attr"; never imported by reading the setting. |

### `resources`

| key | type | default | scope | env | validation | description |
|---|---|---|---|---|---|---|
| `resources.enabled` | boolean | true | both | OZAGIA_RESOURCES_ENABLED | true or false | Observe host resources and apply admission checks. |
| `resources.sample_s` | number | 2.0 | both |  | a number between 0.1 and 3600 | Sampling interval for host resource observations, in seconds. |
| `resources.watchdog` | string | "auto" | both |  | one of auto, cgroup, rlimit, watch or off | Resource enforcement mode. |
| `resources.history` | boolean | true | both |  | true or false | Keep bounded run history for admission estimates. |
| `resources.reserve_memory_pct` | number | 10.0 | both |  | a number between 0 and 80 | Host memory kept free for the OS and the user, in percent. |
| `resources.reserve_disk_pct` | number | 0.0 | both |  | a number between 0 and 80 | Optional proportional disk reserve in percent; 0 disables it. Prefer reserve_disk_gb and the requested/expected disk size. |
| `resources.reserve_disk_gb` | number | 1.0 | both |  | a number between 0 and 1000000 | Disk space kept free for the OS and the user, in GiB. |
| `resources.min_free_memory_pct` | number | 15.0 | both |  | a number between 0 and 100 | Minimum free memory required before new work starts, in percent. |
| `resources.min_free_disk_gb` | number | 2.0 | both |  | a number between 0 and 1000000 | Minimum free disk required before new work starts, in GiB. |
| `resources.wait_max_s` | integer | 300 | both |  | an integer between 0 and 86400 | Longest explicit wait for resources, in seconds. |
| `resources.job_memory_mb` | integer | 2048 | both |  | an integer between 1 and 16777216 | Default memory limit for a job or bounded task, in MiB. |
| `resources.job_disk_mb` | integer | 4096 | both |  | an integer between 1 and 16777216 | Default disk budget for a job or bounded task, in MiB. |
| `resources.job_pids` | integer | 256 | both |  | an integer between 1 and 1000000 | Default process-count limit for a job or bounded task. |

### `worktrees`

| key | type | default | scope | env | validation | description |
|---|---|---|---|---|---|---|
| `worktrees.root` | string | "../ozagia-wt" | repo | OZAGIA_WORKTREES_ROOT | a repository-relative or absolute directory path | Default directory for feature worktrees, relative to the repository or absolute. |
| `worktrees.min_free_gb` | number | 5.0 | both |  | a number between 0 and 1000000 | Free disk required before creating or using a worktree, in GiB. |
| `worktrees.avoid_tmpfs` | boolean | true | both | OZAGIA_WORKTREES_AVOID_TMPFS | true or false | Refuse or warn when a worktree is on tmpfs. |

### `reads`

| key | type | default | scope | env | validation | description |
|---|---|---|---|---|---|---|
| `reads.structured_outline_lines` | integer | 2000 | repo |  | an integer between 0 and 10000000 | Line limit for a structured read with no range; 0 turns it off. |
| `reads.structured_outline_bytes` | integer | 65536 | repo |  | an integer between 0 and 1000000000 | Byte limit for a structured read with no range; 0 turns it off. |
| `reads.outline_lines` | integer | 400 | repo |  | an integer between 0 and 10000000 | Line limit for an ordinary text read with no range; 0 turns it off. |
| `reads.outline_bytes` | integer | 40960 | repo |  | an integer between 0 and 1000000000 | Byte limit for an ordinary text read with no range; 0 turns it off. |
| `reads.repeat` | string | "note" | repo |  | one of stub, note or off | What a same-session re-read of an unchanged range gets. |

### `markdown`

| key | type | default | scope | env | validation | description |
|---|---|---|---|---|---|---|
| `markdown.history` | string | "history" | repo | OZAGIA_MARKDOWN_HISTORY | a repository-relative directory, not a glob | Versioned directory for dated one-off Markdown. |
| `markdown.history_index` | boolean | true | repo |  | true or false | Generate an index for the history directory. |
| `markdown.new_file_budget` | integer | 3 | both |  | an integer between 0 and 1000 | New Markdown files allowed per session or task before a reason is required. |
| `markdown.duplicate_threshold` | number | 0.85 | both |  | a number between 0 and 1 | Similarity above which a new note is reported as a near-duplicate. |
| `markdown.root_allow` | string_list | ["README.md", "CHANGELOG.md", "LICENSE", "CONTRIBUTING.md", "AGENTS.md", "SECURITY.md"] | repo |  | root-level file names | Root-level Markdown files that may exist outside the routing rules. |

### `files`

| key | type | default | scope | env | validation | description |
|---|---|---|---|---|---|---|
| `files.ledger` | boolean | true | both |  | true or false | Record files created or changed by ozagia sessions. |
| `files.trash` | string | ".ozagia/trash" | repo |  | a repository-relative path | Repository-relative directory that cleanup moves files to. |
| `files.retention_days` | integer | 30 | both |  | an integer between 0 and 36500 | Days to keep scratch or trash entries before cleanup suggests removal. |

### `models`

| key | type | default | scope | env | validation | description |
|---|---|---|---|---|---|---|
| `models.default` | string | "" | both | OZAGIA_MODELS_DEFAULT | a model id, or empty for the host default | Default model id as provider/model; empty uses the host default. |
| `models.fallback` | string | "" | both |  | a model id, or empty for no fallback | Fallback model id used when the default is unavailable; empty disables it. |
| `models.local_only` | boolean | false | both | OZAGIA_MODELS_LOCAL_ONLY | true or false | Refuse remote model providers. |

### `tasks`

| key | type | default | scope | env | validation | description |
|---|---|---|---|---|---|---|
| `tasks.default_budget_tokens` | integer | 40000 | both |  | an integer between 1 and 1000000000 | Default token budget for a bounded task. |
| `tasks.max_wall_s` | integer | 900 | both | OZAGIA_TASKS_MAX_WALL_S | an integer between 1 and 604800 | Default wall-clock limit for a bounded task, in seconds. |

### `review`

| key | type | default | scope | env | validation | description |
|---|---|---|---|---|---|---|
| `review.min_reviewers` | integer | 1 | both |  | an integer between 0 and 16 | Minimum independent reviewers for a review run. |
| `review.require_clean` | boolean | true | both |  | true or false | Require a clean worktree before review or merge checks. |
| `review.layers` | string_list | [] | repo |  | repository-relative directories (may be empty) | Path prefixes from the lowest layer to the highest for layering checks. |

### `migration`

| key | type | default | scope | env | validation | description |
|---|---|---|---|---|---|---|
| `migration.python` | string | "" | repo |  | a repository-relative directory or empty | Python source tree for the migration overlay; empty disables it. |
| `migration.rust` | string | "" | repo |  | a repository-relative directory or empty | Rust source tree for the migration overlay; empty disables it. |
| `migration.infer` | boolean | false | repo |  | true or false | Report name/path migration candidates; never accepts them. |

### `testing`

| key | type | default | scope | env | validation | description |
|---|---|---|---|---|---|---|
| `testing.mode` | string | "affected" | both | OZAGIA_TESTING_MODE | one of none, smoke, affected, package, all | Default test-selection mode for bounded tasks. |
| `testing.fallback` | string | "all" | both | OZAGIA_TESTING_FALLBACK | one of all, refuse, affected_with_warning | What a plan does when a changed file has no linked test or the graph is missing or stale. |
| `testing.max_tests` | integer | 50 | both | OZAGIA_TESTING_MAX_TESTS | an integer between 1 and 100000 | Maximum number of test files a plan selects. |
| `testing.timeout_s` | integer | 300 | both | OZAGIA_TESTING_TIMEOUT_S | an integer between 1 and 86400 | Timeout a caller should apply to the planned test run, in seconds. |
| `testing.captured` | string | "all" | both | OZAGIA_TESTING_CAPTURED | one of none (or off), smoke, affected, package, all | Default mode for an interactive captured pytest; off is an alias for none and disables selection. |
| `testing.cache` | boolean | false | both |  | true or false | Skip a selected test whose inputs are unchanged since it passed. |

### `lint`

| key | type | default | scope | env | validation | description |
|---|---|---|---|---|---|---|
| `lint.enabled` | boolean | true | repo |  | true or false | Lint changed files after a handled edit; the legacy loader treats an unset value as automatic. |
| `lint.timeout_s` | number | 3.0 | repo |  | a number between 0.001 and 600 | Budget for the linter runs of one call, in seconds. |
| `lint.python` | string | "auto" | repo |  | one of auto, ruff or off | Python linter: auto, ruff or off. |
| `lint.rust` | string | "auto" | repo |  | one of auto, rustfmt or off | Rust linter: auto, rustfmt or off. |
| `lint.trust_project_venv` | boolean | false | user |  | true or false | Allow linters from inside the repository (user config only). |

### `environments`

| key | type | default | scope | env | validation | description |
|---|---|---|---|---|---|---|
| `environments.trust_project_python` | boolean | false | user |  | true or false | Allow running the project's own Python interpreters to probe them (user config only; docs/environments.md). |

### `jobs`

| key | type | default | scope | env | validation | description |
|---|---|---|---|---|---|---|
| `jobs.enabled` | boolean | false | repo |  | true or false | Allow background jobs in the daemon for this repository. |

### `iac`

| key | type | default | scope | env | validation | description |
|---|---|---|---|---|---|---|
| `iac.roots` | string_list | [] | repo |  | repository-relative directories (may be empty) | Explicit Terraform/OpenTofu roots. |
| `iac.skip_globs` | string_list | [] | repo |  | repository-relative glob patterns (may be empty) | Extra IaC source files or directories to skip; built-in backup globs still apply. |

### `security`

| key | type | default | scope | env | validation | description |
|---|---|---|---|---|---|---|
| `security.enabled` | boolean | true | repo |  | true or false | Enable the security and antipattern scan for this repository. |
| `security.tools` | string_list | [] | repo |  | one of the fixed tool registry names | Explicit opt-in offline security tools. |
| `security.allow_network` | boolean | false | repo |  | true or false | Allow a security tool to refresh a remote database. |
| `security.fail_on` | string | "high" | repo |  | one of blocking, high, medium, low or never | Severity at which a security scan reports a blocking result. |
| `security.exclude` | string_list | ["tests/**", "test/**", "docs/**", "vendor/**"] | repo |  | repository-relative glob patterns | Path globs excluded from security findings. |
| `security.max_findings` | integer | 200 | repo |  | an integer between 1 and 10000 | Cap the number of security findings a scan reports. |
| `security.block_secrets` | boolean | true | repo |  | true or false | Block staged or pushed secrets by default. |
| `security.secret_allowlist` | string_list | [] | repo |  | path:, rule: or sha256: entries | Secret-gate allowlist entries: path:, rule: or sha256: patterns. |
| `security.finding_allowlist` | string_list | [] | repo |  | exact sha256:<16 hex> finding fingerprints (path:/rule: accepted but discouraged) | Reviewed allowlist for intentional static scan findings; prefer exact sha256 finding fingerprints over broad path: or rule: entries. |

### `ui`

| key | type | default | scope | env | validation | description |
|---|---|---|---|---|---|---|
| `ui.default_aperture` | string | "architecture" | both |  | a lowercase aperture id | Aperture shown first in a graph view. |
| `ui.theme` | string | "auto" | both | OZAGIA_UI_THEME | one of auto, light or dark | Graph and console theme. |

### `hints`

| key | type | default | scope | env | validation | description |
|---|---|---|---|---|---|---|
| `hints.library` | string | "gentle" | both | OZAGIA_HINTS_LIBRARY | one of off, gentle or strict | Library-first hints after an edit: off, gentle (new/changed symbols) or strict (also existing symbols in the edited files). |
| `hints.max_per_edit` | integer | 2 | both | OZAGIA_HINTS_MAX_PER_EDIT | an integer between 0 and 20 | Maximum library hints shown per edited file. |
| `hints.record` | boolean | true | both | OZAGIA_HINTS_RECORD | true or false | Record shown and suppressed library hints in the state directory. |
| `hints.ecosystems` | string_list | ["python", "javascript", "rust"] | both | OZAGIA_HINTS_ECOSYSTEMS | python, javascript or rust (may be empty) | Ecosystems inspected for library hints; TypeScript counts as javascript. |

## `.ozagia.toml` — resolved model (43 fields)

Fields of `ozagia.config.ProjectConfig` after resolution (precedence, legacy migration and registry mapping applied).

| field | type | default |
|---|---|---|
| `root` | `Path` | — |
| `source` | `Path | None` | — |
| `source_dirs` | `tuple[str, ...]` | — |
| `map_dirs` | `tuple[str, ...]` | — |
| `test_dirs` | `tuple[str, ...]` | — |
| `skip_dirs` | `frozenset[str]` | — |
| `memory_skip_dirs` | `frozenset[str]` | — |
| `docs` | `tuple[str, ...]` | — |
| `docs_all` | `tuple[str, ...]` | — |
| `key_docs` | `tuple[str, ...]` | — |
| `command_docs` | `tuple[str, ...]` | — |
| `import_roots` | `tuple[str, ...]` | — |
| `path_prefixes` | `tuple[str, ...]` | — |
| `changed_exclude` | `tuple[str, ...]` | — |
| `entrypoint_prefixes` | `tuple[str, ...]` | — |
| `adapters` | `tuple[str, ...]` | — |
| `structured_outline_lines` | `int` | 2000 |
| `structured_outline_bytes` | `int` | 65536 |
| `outline_lines` | `int` | 400 |
| `outline_bytes` | `int` | 40960 |
| `repeat` | `str | None` | None |
| `repeat_by_harness` | `tuple[tuple[str, str], ...]` | () |
| `jobs_enabled` | `bool` | False |
| `tests_select` | `str` | 'all' |
| `tests_cache` | `bool` | False |
| `lint_enabled` | `bool | None` | None |
| `lint_timeout_s` | `float` | 3.0 |
| `lint_python` | `str | None` | None |
| `lint_rust` | `str | None` | None |
| `iac_roots` | `tuple[str, ...]` | () |
| `iac_skip_globs` | `tuple[str, ...]` | ('*.backup.last', '*.backup', '*.bak', '*.orig') |
| `remote_states` | `tuple[RemoteStateConfig, ...]` | () |
| `environments` | `tuple[EnvironmentConfig, ...]` | () |
| `services` | `tuple[ServiceConfig, ...]` | () |
| `relays` | `tuple[RelayConfig, ...]` | () |
| `runtime_services` | `tuple[RuntimeServiceConfig, ...]` | () |
| `runtime_tunnels` | `tuple[RuntimeTunnelConfig, ...]` | () |
| `review_layers` | `tuple[str, ...]` | () |
| `migration` | `MigrationConfig` | MigrationConfig(python=None, rust=None, infer=False, pairs=()) |
| `sources` | `tuple[Path, ...]` | () |
| `source_signatures` | `tuple[tuple[str, str, int, int, str], ...]` | () |
| `unmapped` | `tuple[tuple[str, str], ...]` | () |
| `adapter_base_dir` | `Path | None` | None |
