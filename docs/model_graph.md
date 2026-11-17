# Model graph, similarity and advisory matching

`ozagia.core.model_graph` adds an approximate map around the model registry in
`ozagia.core.models`. It answers questions such as:

- Which family, type and size class does a discovered model belong to?
- Which models are nearest to this one in model space?
- Which available models fit a code-review, PDF or document task?
- Which tool format fits a model best?

It is **advisory**. It never calls a model, contacts a provider or changes
`models.resolve`. Nothing is routed automatically. The core module is
standard-library only, and the profiles are curated approximations rather than
authoritative provider metadata.

```python
from ozagia.core import model_graph

graph = model_graph.graph(root)                  # bounded nodes + edges
near = graph.neighbours("openai/gpt-5", limit=3)
req = model_graph.requirements(root, "diff_review")
result = model_graph.match(root, req, limit=5)   # ranked, advisory
```

## Node schema

One `ModelNode` is built for each record in `models.catalog(root)`, so the
registry still decides which models exist, their availability and their
locality. The graph adds profile metadata:

| Field | Meaning |
| --- | --- |
| `id`, `provider`, `model` | The OpenCode id and its split parts. |
| `family` | Base family such as `claude`, `gpt`, `gemini`, `llama`, `qwen`, `mistral`, `deepseek`, `gemma`, `phi`, `grok`, `command`, `nemotron`; an inferred name otherwise. |
| `variant` | The variant within the family: `opus`, `sonnet`, `haiku`, `mini`, `flash`, `pro`, `coder`, `r1`, a parameter size such as `70b`, or `default`. |
| `type` | Primary mode: `chat`, `reasoning`, `code`, `multimodal`, `embedding`, `image` or `audio`. |
| `size_class` | `tiny`, `small`, `medium`, `large`, `frontier` or `unknown`. |
| `params_total_b` | Total parameters in billions, or `null` when undisclosed. |
| `params_active_b` | Active parameters for a mixture-of-experts model, or `null`. |
| `context` | Context window in tokens, or `null` when unknown. |
| `input_cost`, `output_cost` | USD per million tokens, or `null` when unknown. |
| `modalities_in`, `modalities_out` | `text`, `image`, `pdf`, `audio`, `video`, `embedding`. |
| `capabilities` | `tools`, `vision`, `audio`, `pdf_native`, `long_context`, `structured_output`, `computer_use`, `code`, `reasoning`. |
| `tool_formats` | `apply_patch`, `replace`, `whole_file`, `library_call`, `cli`, `mcp`, `native_pdf`. |
| `local`, `remote` | `true`/`false` only when configuration declares locality, else `null`. |
| `status` | `available` (listed by `opencode models`) or `unknown`. |
| `source` | `profile`, `override`, `external` or `inferred`; a cached source id (for example `models-dev`) when imported metadata supplied the node. |
| `confidence` | `curated`, `declared`, `external` or `inferred`. |
| `profile_source` | The short source note of the matched profile, or the cached source/provenance note. |
| `notes` | A short caveat, for example "closed weights; parameter count undisclosed". |
| `catalog_source` | How the record entered the catalog: `opencode`, `settings`, `config` or `override`. |

`to_dict()` is the JSON shape used by the CLI and reports.

## Families, types and sizes

A family is a base model line, not necessarily a provider. `openai/gpt-5` and
`openai/codex-mini` are both family `gpt`; `anthropic/claude-opus-4-1` and
`anthropic/claude-sonnet-4-5` are both `claude` with different variants. The
provider stays a separate edge in the graph.

The type records the primary mode. Claude is `reasoning` even though it accepts
images and PDFs; the modalities and capabilities carry that information. A
model that is both text and image is `multimodal` when that is its defining
mode, and `chat` otherwise.

Size classes are deliberately coarse:

| Size class | Typical meaning |
| --- | --- |
| `tiny` | under about 4B parameters, or a nano/mini closed tier |
| `small` | about 4B to 15B, or a small closed tier |
| `medium` | about 15B to 80B, or a mid closed tier |
| `large` | about 80B to 400B, or a large closed tier |
| `frontier` | 400B and above, or a flagship closed tier |
| `unknown` | no reliable size |

Closed families use `size_class` and leave `params_total_b`/`params_active_b`
`null`. Open-weight profiles include numeric parameters only where the value is
public. An inferred node parses an explicit size token from the id (for example
`7b` or `30b-a3b`) but does not guess a context window or a price.

## Profiles and precedence

Built-in profiles live in the packaged
`src/ozagia/core/model_profiles.toml`. They use `fnmatch` patterns against the
lower-case model name, specific variants first and a generic family profile
last. The file also records a short `source` and human `notes` per profile.

Node metadata precedence is:

```text
exact [model_graph.models."provider/model"] override
  > external metadata passed to graph(..., external=...)
  > repository [model_graph.families] pattern
  > built-in model_profiles.toml pattern
  > inferred from provider/model id
```

External metadata is a plain mapping of model id to the same supported fields.
It is merged after the curated/inferred layer and before explicit repository
overrides. Nothing is fetched: a later cached importer passes the mapping in.
Unknown external fields are reported like unknown configuration keys; known
fields with an invalid type are rejected.

Task requirements have their own precedence:

```text
exact model override (for node metadata)
  > repository [model_graph.tasks.<class>] override
  > built-in task profile
  > explicit requirements (which extend capabilities/tool formats)
```

The task layer does not rename a model's family. It changes the requirements
used by `requirements()` and `match()`. Unknown ids still get an inferred
family and `confidence: "inferred"`; they are not assigned invented parameter
counts, context or prices.

### External metadata

`graph`, `neighbours`, `match`, `tool_fit` and `tool_matrix` accept an optional
`external=` mapping:

```python
external = {
    "openai/gpt-5": {
        "context": 400000,
        "input_cost": 1.25,
        "output_cost": 10.0,
        "modalities_in": ["text", "image", "pdf"],
    },
}
graph = model_graph.graph(root, external=external)
```

Each value accepts the same fields as a `[model_graph.models]` override,
including `profile_source`. A field present externally replaces the curated or
inferred value; fields it omits keep the curated value, or stay `null` when
nothing is known. `null` is never turned into a guessed number. When external
metadata supplies a parameter count but no size class, the coarse size class is
derived from that count. An explicit repository override still wins for every
field it sets. Nodes that received external fields report `source: "external"`
and `confidence: "external"` unless the mapping sets those fields itself; an
explicit override reports `source: "override"` and `confidence: "declared"`.

### Cached data sources

`ozagia.core.model_sources` implements the explicit cache/import seam for
models.dev, LiteLLM, OpenRouter and Epoch AI. `graph`, `similar`, `match` and
`show` load valid cached snapshots by default and pass the merged mapping as
`external=`. Use `--source SOURCE` to select one or more, or `--no-sources` to
use curated profiles only. Cached metadata never changes `models.resolve`.

```bash
ozagia-models sources --root .
ozagia-models sync models-dev --allow-network --root .   # explicit opt-in
ozagia-models import models-dev ./snapshot.json --root . # offline
ozagia-models graph --source models-dev --root . --json
```

The merge precedence is `models-dev` > `litellm` > `openrouter` > `epoch`, with
field-level gap filling. Missing, malformed and stale caches are reported in a
`sources` object and are never fetched while building a graph. See
[model_sources.md](model_sources.md) for the source table, licences, cache
metadata, commands, field mapping and limitations.

## Distance

`distance(a, b, weights=None)` accepts two `ModelNode` objects (or their
mappings) and returns:

```json
{"distance": 0.2145, "similarity": 0.7855,
 "components": {"family": 1.0, "variant": 0.0, "...": 0.0},
 "weights_used": {"family": 0.2, "...": 0.08},
 "coverage": 0.92,
 "missing": ["params"]}
```

It is a weighted Gower-style indicator over known features:

| Feature | Comparison |
| --- | --- |
| `family` | equal / not equal |
| `variant` | equal / not equal |
| `type` | equality with a small affinity table between chat, reasoning, code and multimodal |
| `size` | ordinal over tiny/small/medium/large/frontier; `unknown` is skipped |
| `params` | `log1p(total or active)` normalized at 2000B |
| `context` | `log1p(tokens)` normalized at 10M |
| `cost` | `log1p` of the mean known input/output cost, normalized at $1000/M |
| `capabilities` | Jaccard |
| `modalities` | Jaccard over `in:`/`out:` modality pairs |
| `tool_formats` | Jaccard |

Default weights are:

```text
family .20  variant .08  type .12  size .12  params .08
context .08  cost .06  capabilities .12  modalities .07  tool_formats .07
```

The distance is `1 - weighted average similarity` over the features known for
both nodes. `coverage` is the known weight divided by total effective weight,
so it says how much of the comparison was actually possible. Identical ids are
exactly `0.0` with full coverage. The result is symmetric, deterministic and
bounded in `0..1`.

Caveats:

- Missing metadata is skipped, not imputed. A pair with only `family` known is
  not comparable to a pair with every field known.
- Because coverage varies by pair, this is a **similarity indicator, not a
  strict metric**. Triangle inequality and transitivity do not hold.
- Setting a weight to `0` disables that feature. Weights are merged over the
  defaults; omitted weights keep their default.
- Curated prices and context values are approximate and may be stale.

## Graph edges

`graph(root, ...)` returns nodes plus bounded edges:

| Kind | Meaning | Edge weight |
| --- | --- | --- |
| `family` | Consecutive nodes of the same family, sorted by id | 1.0 |
| `provider` | Consecutive nodes from the same provider | 0.75 |
| `similarity` | Undirected nearest-neighbour edges above the threshold and within top-k | the similarity |

Defaults and bounds are `MAX_NODES=500`, `MAX_EDGES=2000`, top-k `3`, and a
similarity threshold of `0.55`. Family and provider edges form a chain rather
than a clique, so a large family stays bounded. `truncated`, `omitted_nodes`
and `omitted_edges` report any cut. Every edge carries `kind`, `source`,
`target`, `weight`, `similarity` and `coverage`.

`neighbours(model_id, limit=5)` recomputes the same distance over the graph and
sorts by `(-similarity, id)`, so ties and repeated runs are deterministic.

## Task requirements and matching

`requirements(task=None, capabilities=(), tool_formats=(), local_only=False,
min_context=None, max_cost=None, size=None, type=None)` returns a
`Requirements` value. Built-in task profiles exist for:

| Task | Key requirements |
| --- | --- |
| `code_review` | tools, code, reasoning, long context; apply-patch/replace; 64k context |
| `diff_review` | tools, code, structured output; apply-patch/replace; 32k |
| `paper_triage` | PDF, long context, reasoning, structured output; native PDF; 128k |
| `repo_scan` | tools, code, long context; CLI/library; 64k |
| `test_triage` | tools, code, reasoning; apply-patch/CLI; 32k |
| `pdf_edit` | PDF, vision, tools, structured output; native PDF; 128k |
| `document_extract` | PDF/image, vision, long context, structured output; 128k |
| `image_review` | vision, reasoning, structured output; image input; 32k |
| `research_summarize` | long context, reasoning, structured output; 128k |
| `model_classify` | structured output, reasoning; advisory only |
| `module_build` | tools, code, long context, structured output; 64k |
| `log_triage` | tools, code, structured output, long context; 64k |
| `security_review` | tools, code, reasoning, long context; 128k |

Explicit capabilities and tool formats extend the task profile. Repository
`[model_graph.tasks.<class>]` tables replace the built-in values for the keys
they set.

`match(root, requirements, limit=5)` applies these **hard filters**:

- `local_only`: the model must be explicitly declared local;
- `min_context`: the context must be known and at least the minimum;
- `max_cost`: the cost must be known and at most the maximum;
- every required capability;
- every required input/output modality;
- an explicitly required model type.

A model that passes the filters but has `status: "unknown"` is listed in
`not_assessed` with its status; it is not silently dropped. A model that fails a
hard filter is listed in `excluded` with the reason. Only `available` models are
ranked. `size` is a preference (the maximum acceptable class), not a hard
filter.

Ranking weights:

```text
capability coverage .30   tool-format coverage .15   type fit .10
size fit .10              context headroom .12        cost .10
availability .08          task family preference .05
```

Each candidate reports `score`, `components`, `reasons`, its family/type/size,
status, capabilities and tool formats. Ordering is by score descending and id
ascending. The result also includes `source: "advisory"`, `task`,
`requirements`, `selected`, `candidates`, `excluded`, `not_assessed`,
`discovery`, `warnings` and unknown configuration keys.

### Examples

Code review:

```bash
ozagia-models match --task code_review --root .
ozagia-models match --task diff_review --capability structured_output \
  --tool-format apply_patch --min-context 64000 --root .
```

PDF work:

```bash
ozagia-models match --task pdf_edit --root .
ozagia-models match --task document_extract --modality pdf \
  --type multimodal --min-context 128000 --root .
```

The second form uses the PDF profile and then narrows to multimodal models with
enough context. The result is advice: review the reasons and still use
`models.resolve` for the deterministic configured choice.

## Configuration overrides

All graph overrides are read with
`ozagia.config.load_config_section(root, "model_graph")`, so the legacy
`.ozagia.toml` wins for keys it defines and `ozagia/config.toml` fills the rest.
No settings-registry registration is required.

```toml
[model_graph.models."openai/gpt-5"]
context = 400000
capabilities = ["tools", "vision", "long_context", "pdf_native"]
tool_formats = ["replace", "whole_file", "native_pdf"]
notes = "repository override"

[model_graph.families.acme]
patterns = ["*acme-*", "*fable*"]
type = "code"
size_class = "medium"
source = "repository family declaration"

[model_graph.tasks.diff_review]
capabilities = ["tools", "code"]
tool_formats = ["apply_patch", "replace"]
min_context = 64000

[model_graph.weights]
family = 0.25
capabilities = 0.10
cost = 0.0
```

Supported model fields are `family`, `variant`, `type`, `size_class`,
`params_total_b`, `params_active_b`, `context`, `input_cost`, `output_cost`,
`modalities_in`, `modalities_out`, `capabilities`, `tool_formats`, `local`,
`source`, `confidence`, `notes` and `profile_source`. Family declarations use
`patterns` plus the same metadata fields. Unknown keys are reported in the
graph report under `unknown_keys` and as warnings; they are not fatal. A known
field with an invalid type or range is a `ModelGraphError`.

## Tool fit

`tool_fit(root, model_id, tools)` accepts plain descriptors or objects with
`id`, `format`, `provides` and `preferred_by` attributes, so a future toolchain
module can pass its own values:

```python
model_graph.tool_fit(root, "openai/gpt-5", [
    {"id": "patch", "format": "apply_patch", "provides": ["code"]},
    {"id": "pdf", "format": "native_pdf", "provides": ["pdf_native"]},
])
```

Each combination is annotated with format match (direct, near-equivalent or
unsupported), capability coverage and preference match, then ordered by fit.
`tool_matrix(root, tools)` ranks pairs across the graph. No tool is executed.

## Commands

```text
ozagia-models graph [--root R] [--json] [--max-nodes N] [--max-edges N]
                    [--top-k K] [--threshold F]
                    [--source SOURCE]... [--no-sources]
ozagia-models show MODEL [--root R] [--json]
                   [--source SOURCE]... [--no-sources] [--with-metadata]
ozagia-models similar MODEL [--limit N] [--root R] [--json]
                     [--source SOURCE]... [--no-sources]
ozagia-models match [--task CLASS] [--capability X]... [--tool-format F]...
                    [--local] [--min-context N] [--max-cost N]
                    [--size CLASS] [--type TYPE] [--modality M]...
                    [--limit N] [--root R] [--json]
                    [--source SOURCE]... [--no-sources]
ozagia-models sources [--root R] [--json]
ozagia-models sync SOURCE [--allow-network] [--timeout S] [--max-bytes N]
                        [--root R] [--json]
ozagia-models import SOURCE PATH [--root R] [--json]
```

Existing `list` and `resolve` commands are unchanged. `show` keeps the
registry JSON shape and adds a `profile` object; `--with-metadata` (or an
explicit `--source`) also adds the cached `metadata` and a `sources` report.
`show` uses valid caches by default; `--no-sources` keeps the curated view.
Text output adds the graph fields and, with metadata, the provenance. `graph`
text output is capped at 40 nodes and 40 edges for readability; `--json`
contains the bounded graph and a `sources` object. `similar` and `match` print
compact, model-facing lines with ids, family/type/size, status, score and
reasons, followed by a source-cache summary.

`graph`, `similar`, `match` and `show` use valid cached metadata by default
in the documented precedence order. `--no-sources` disables it. `sync` refuses
to open a socket without `--allow-network`; `import` is offline.

Exit status is `0` on success and `2` for validation, configuration or IO
errors, including an unknown model id for `show`/`similar` and an invalid
`--size`/`--max-nodes`.

## Limits and gaps

- Profiles are curated and approximate. A provider may change pricing,
  context, modalities or tool support without notice.
- Closed models keep `params_total_b`/`params_active_b` `null`; undisclosed
  context and cost also stay `null`. The graph does not infer them from a name.
- Matching is advisory and does not call `models.resolve`, a classifier or a
  model. Auto-routing remains out of scope.
- `opencode models` only lists ids. Everything else comes from curated
  profiles, explicit overrides or an explicitly cached metadata source
  ([model_sources.md](model_sources.md)). The graph never fetches a cache.
- Cached metadata can be stale; `sources` reports age and staleness, and
  `--no-sources` returns to the curated-only view.
- The graph is bounded by node and edge counts; very large catalogs are
  truncated with a reported count.
- The toolchain module is not present yet. `tool_fit` deliberately accepts
  plain dictionaries so it can integrate later without a dependency.

## Tests

```bash
PYTHONPATH=src python -m pytest -q tests/test_model_graph.py \
  tests/test_model_sources.py tests/test_models.py
ruff check src tests
```

Tests inject the discovery runner or patch `default_runner`; no test runs the
real `opencode` CLI, contacts a provider or calls a model. They cover profile
matching and overrides, missing metadata, distance symmetry/coverage, bounded
edges and deterministic neighbours, hard filters, task profiles, tool fit,
unknown/unavailable reporting, cache adapters/import/sync and CLI JSON/text
shapes.
