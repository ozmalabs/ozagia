# Model registry and per-task defaults

`ozagia.core.models` is a read-only registry of the models the local OpenCode
CLI advertises, overlaid with the explicit `[models]` configuration. It answers
"which models exist?", "which one is configured for this task class?" and "why
was this one selected?".

It is **not** an autonomous router. It has no classifier, makes no network
request and calls no model. Resolution is deterministic and reportable. Auto-
routing waits for an offline evaluation that beats the explicit default on
success rate and cost (plan item 12).

The advisory model graph — curated families, model-space distance, bounded
similarity edges and task/tool matching — lives in `ozagia.core.model_graph`
and is documented in [model_graph.md](model_graph.md). It does not change
`models.resolve`. Cached provider metadata (models.dev, LiteLLM, OpenRouter
and Epoch AI) is an explicit, opt-in layer documented in
[model_sources.md](model_sources.md); it is never fetched while listing or
resolving a model.

The module is standard-library only and does not import the target project.
`python -m ozagia.models_cli` is the command entry point until the main CLI is
wired; no console script is installed by this slice.

## Discovery sources

| Source | Where it comes from | What it contributes |
| --- | --- | --- |
| `opencode` | `opencode models` (read-only listing) | Model ids and `status: available`. |
| `settings` | `models.default` / `models.fallback` in the user or repository config | Configured references, even when discovery does not list them. |
| `config` | `[models.tasks]`, `[models.by_task_class]`, `[models.local]` | Per-task pins and explicit locality. |
| `override` | the API `overrides=` argument | Highest-precedence values and records for test/integration callers. |

`opencode models` prints one `provider/model` per line. Discovery runs through
an injected runner:

```python
import subprocess
from ozagia.core import models

def runner(argv, timeout_s):
    return subprocess.CompletedProcess(argv, 0, "local/qwen\nopenai/gpt-5\n", "")

catalog = models.catalog(root, runner=runner)
```

The default runner is `subprocess.run(["opencode", "models"], ...)` with a
60-second timeout and stdin closed. It never runs an OpenCode session or a
model prompt. Tests inject the runner or mock `subprocess.run`; they never need
the real CLI.

Discovery is tolerant but not inventive:

- missing executable, timeout or another `OSError` -> `status: unavailable`;
- non-zero exit, no output, only unparseable lines -> `status: error`;
- a line that is not a valid id -> a warning and that line is ignored;
- ANSI escapes, blank lines and `#` comments are ignored;
- configured models stay in the catalog with `status: unknown` when the CLI
  cannot verify them.

The listing command provides only ids. `context`, `input_cost`, `output_cost`
and `capabilities` are `null`/empty because the command does not report them.
The registry never guesses these values from a model name.

## Configuration

Only the `[models]` table of these files is read:

- the user config: `$OZAGIA_USER_CONFIG` or `~/.config/ozagia/config.toml`;
- the repository config: `<root>/ozagia/config.toml`.

Other sections are left to their existing loaders.

```toml
[models]
default = "openai/gpt-5"
fallback = "ollama/qwen"
local_only = false

[models.tasks]
diff_review = "openai/gpt-5-mini"
paper_triage = "ollama/qwen"

# Alias for [models.tasks]; use one or the other, not both.
# [models.by_task_class]
# diff_review = "openai/gpt-5-mini"

[models.local]
providers = ["ollama", "lmstudio"]
models = ["vllm/private-model"]
```

- `default` is the model for any task class without a pin. Empty means "the
  host CLI chooses", reported as `host_default`, not as a guessed model.
- `fallback` is used only when the selected model is unavailable or filtered
  by `local_only`. Empty means no fallback. It is never an arbitrary second
  model discovered at runtime.
- `local_only` refuses models that are not explicitly declared local.
- A task class may pin a model or accept the default. Unknown task classes do
  not fail; they use the default.
- `[models.local]` is how locality is declared. Discovery does not report it,
  so an undeclared model has `local: null` and `remote: null`, and is filtered
  when `local_only` is true. This is deliberate: a remote gateway named
  `ollama` is not assumed local.
- Model ids are bounded OpenCode ids (`provider/model`), including real ids
  with brackets or variants such as `claude-agent/claude-fable-5-1[1m]`.
  Whitespace, control characters and ids without a provider are rejected.
- Unknown keys in `[models]`, `[models.tasks]` or `[models.local]` are errors
  with a close suggestion. A configured model that discovery does not list is
  reported, not silently accepted.

Precedence follows `ozagia.core.settings`:

```text
built-in default < user config < repository config < environment < override
```

Environment values:

| Variable | Setting |
| --- | --- |
| `OZAGIA_MODELS_DEFAULT` | `models.default` (empty means the host default) |
| `OZAGIA_MODELS_LOCAL_ONLY` | `models.local_only` |

Task entries merge by task class: a repository pin replaces a user pin for the
same class and leaves other classes intact. Local providers and models are
unioned across layers. Every effective value carries its layer in
`ModelSettings.sources`; each task pin carries its layer in `task_sources`.

The typed settings registry validates the same dynamic tables, so
`ozagia config values` and `ozagia config set models.tasks.<class> ...` accept
a config containing `[models.tasks]`, `[models.by_task_class]` and
`[models.local]`. `settings.model_overrides(root)` returns the effective
`tasks` and `local` maps from that registry. The two loaders are tested for
consistency; `core.models` remains the source of per-layer evidence and
resolution.

## API

```python
from ozagia.core import models

catalog = models.catalog(root)                  # Catalog
record = models.get(root, "openai/gpt-5")       # ModelRecord | None
choice = models.resolve(root, "diff_review")    # Resolution
```

`Catalog` is iterable and indexable. It reports discovery status, warnings,
settings and the merged records. `ModelRecord` has:

| Field | Meaning |
| --- | --- |
| `id` | `provider/model` |
| `provider`, `model` | The id split at the first `/` |
| `context` | Context size in tokens, or `null` when unknown |
| `input_cost`, `output_cost` | USD per million tokens, or `null` when unknown |
| `capabilities` | Sorted capability names; empty when unknown |
| `local` / `remote` | `true`/`false` when declared, else `null` |
| `source` | `opencode`, `config`, `settings` or `override` |
| `status` | `available` (listed) or `unknown` (not verified) |
| `references` | Config entries naming it: `default`, `fallback`, `task:<class>`, `local` |

`source` says how the record entered the catalog: discovery introduces ids as
`opencode`; a missing configured id is added as `settings` (default/fallback),
`config` (task pin or local declaration) or `override`. `references` keeps the
configuration provenance when a discovered model is also configured.

### Resolution

`resolve(root, task_class=None)` returns a `Resolution` with `requested`,
`selected`, `source`, `status`, `reason`, `fallback_used`, `candidates`,
`discovery`, `settings` and `warnings`.

The order is:

1. the task class's explicit pin, when one exists;
2. otherwise `models.default`;
3. if the first choice is unavailable or filtered by `local_only`, the
   explicitly configured `fallback` (never an arbitrary model);
4. if nothing is configured, `host_default`: the result has no selected model
   because the host CLI owns that choice.

### Model graph

`ozagia.core.model_graph` maps the same catalog onto curated family, type and
size profiles, a bounded similarity graph and advisory task/tool matching. Use
it to compare models (`similar`), inspect profiles (`graph`) or rank candidates
for `code_review`/`pdf_edit` and related task classes (`match`). It never calls
`models.resolve` or a model, and it never changes the deterministic choice
above. Profiles are approximate; missing metadata stays `null`. See
[model_graph.md](model_graph.md).

`status` values:

| Status | Meaning |
| --- | --- |
| `resolved` | A model was selected and is available (or the fallback is). |
| `unknown` | A configured model was selected, but discovery failed, so availability is not verified. |
| `unresolved` | No usable model: unavailable, filtered and/or no usable fallback. |
| `host_default` | No model is configured; the host CLI chooses. |

`source` is the selection role: `task`, `default`, `fallback` or `host`. The
config layer that supplied the id is `requested_layer`
(`default`/`user`/`repo`/`env`/`override`). Each candidate has its own `role`,
`source`, `layer`, `status` (`available`, `unknown` or `filtered`), `selected`
flag and reason. A candidate filtered by `local_only` says so explicitly.

Discovery failure does not discard a configured choice: it is selected with
`status: unknown`. Under `local_only`, an undeclared locality is still refused,
because the registry cannot prove it is local.

## Commands

```text
python -m ozagia.models_cli list [--root R] [--json]
python -m ozagia.models_cli show MODEL_ID [--root R] [--json]
                                [--source SOURCE]... [--no-sources]
                                [--with-metadata]
python -m ozagia.models_cli resolve [--task CLASS] [--root R] [--json]
python -m ozagia.models_cli graph [--root R] [--json] [--source SOURCE]...
                                  [--no-sources]
python -m ozagia.models_cli similar MODEL_ID [--limit N] [--root R] [--json]
                                    [--source SOURCE]... [--no-sources]
python -m ozagia.models_cli match [--task CLASS] [...] [--root R] [--json]
                                  [--source SOURCE]... [--no-sources]
python -m ozagia.models_cli sources [--root R] [--json]
python -m ozagia.models_cli sync SOURCE [--allow-network] [--timeout S]
                                 [--max-bytes N] [--root R] [--json]
python -m ozagia.models_cli import SOURCE PATH [--root R] [--json]
```

`graph`, `similar`, `match` and `show` are the advisory model-graph commands;
see [model_graph.md](model_graph.md) for their filters and JSON shape. They
use valid cached metadata by default, with `--no-sources` to disable it.`sources`, `sync` and `import` manage the cache documented in
[model_sources.md](model_sources.md). `sync` exits `2` unless
`--allow-network` is given; `import` is offline.

Examples:

```bash
# Every discovered and configured model, with status and source.
python -m ozagia.models_cli list --root .

# Machine-readable registry, including discovery and warnings.
python -m ozagia.models_cli list --root . --json

# One model. A configured-but-unlisted model is shown with status unknown;
# an id that is neither listed nor configured exits 2.
python -m ozagia.models_cli show local/qwen --root .

# Why did diff_review get this model?
python -m ozagia.models_cli resolve --task diff_review --root .
```

Exit status is `0` on success and `2` for validation, configuration or IO
errors. Unavailable discovery is **not** an error: `list` and `resolve` still
exit 0 and report the discovery status, configured records and warnings.

`ozagia config values --root .` shows the same dynamic entries with their
source, and `ozagia config get models.tasks.diff_review` reads one pin. Writes
are preview-first: `ozagia config set models.tasks.diff_review provider/model
--root . --dry-run`.

JSON shapes are versioned with `schema: 1`:

```json
{"schema": 1, "root": "/repo", "discovery": {"status": "available"},
 "settings": {"default": "openai/gpt-5"},
 "models": [{"id": "openai/gpt-5", "status": "available"}], "warnings": []}
{"schema": 1, "root": "/repo", "model": {"id": "openai/gpt-5"},
 "discovery": {"status": "available"}, "warnings": []}
{"schema": 1, "root": "/repo", "task_class": "diff_review",
 "selected": {"id": "openai/gpt-5-mini"}, "source": "task",
 "status": "resolved", "reason": "...",
 "candidates": [{"id": "openai/gpt-5-mini", "role": "task",
                 "status": "available", "selected": true}]}
```

## Classifier seam

The registry is the stable integration point for a later bounded
`model_classify` task (`models.CLASSIFIER_TASK_CLASS`). That task would take
features such as task class, input size, language, graph size, required tools
and privacy level, and return an advisory model id, confidence and reason.

This slice deliberately does not call the classifier and has no auto-routing
path. A caller may combine an advisory result with `overrides=` and still get
the same deterministic `Resolution` shape, source and candidate reasons.
Auto-routing should only become the default after an offline evaluation shows
it beats the explicit default on success rate and cost.

## Limits and integration notes

- `opencode models` supplies ids only; context, cost and capabilities remain
  unknown until cached external metadata is imported (see
  [model_sources.md](model_sources.md)) or an explicit metadata schema is
  added.
- Locality is explicit configuration, not provider-name inference.
- `ozagia/models.toml` is not read yet. Defaults and task pins live in
  `[models]` in `ozagia/config.toml`, as this slice specifies.
- The advisory model graph reads `[model_graph]` directly through
  `load_config_section`; it is not part of the typed settings registry. Its
  profiles are curated approximations and its matching is advisory. Cached
  metadata is explicit; see [model_graph.md](model_graph.md) and
  [model_sources.md](model_sources.md).
- The settings registry validates `[models.tasks]`/`[models.by_task_class]`
  and `[models.local]` as dynamic tables. `core.models` still reads `[models]`
  directly and ignores unrelated sections.
- `max_cost_usd`, provider-configured detection, a persisted registry cache and
  live provider probes are not implemented. Costs are therefore not used to
  filter or rank models.
- There is no automatic fallback chain and no retry policy: only the explicit
  `fallback` is considered.
- Reads never create `ozagia/` or `.ozagia/` and never write.

## Tests

```bash
PYTHONPATH=src python -m pytest -q tests/test_models.py
ruff check src tests
```

The tests inject the discovery runner or mock `subprocess.run`. They cover
parsed and malformed discovery output, unavailable/error discovery, defaults,
fallback, per-task pins, local-only filtering, unknown models, invalid config,
record sources, resolution reasons, and CLI text/JSON and exit status. No test
runs the real `opencode` CLI or calls a model.
