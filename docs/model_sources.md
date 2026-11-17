# Model metadata sources, cache and import

The advisory model graph is useful with curated profiles alone, but provider
metadata makes its context, price, modality and capability fields less
approximate. `ozagia.core.model_sources` imports that metadata **explicitly**
from a small registry of documented sources, validates it, stores a snapshot
under the state directory and merges cached snapshots into
`model_graph.graph(..., external=...)`.

It is deliberately not a background service:

- no request is made while building a graph, matching a model, listing sources
  or reading a cache;
- `sync` requires `--allow-network` (or `allow_network=True`); the CLI refuses
  without it and exits `2`;
- `import` validates a local snapshot offline;
- there is no automatic refresh, scheduler, credential, API key or provider
  login;
- every imported number keeps its source id and `confidence: "external"`.

## Sources

| id | What it supplies | URL | Licence / attribution | Format | Default stale |
| --- | --- | --- | --- | --- | --- |
| `models-dev` | **Primary**: family, type, open weights, release/knowledge dates, context/output limits, provider prices, modalities and capability flags. | `https://models.dev/catalog.json?type=all` fallback `https://models.dev/api.json?type=all` | MIT; models.dev contributors | JSON | 7 days |
| `litellm` | Broad fallback: context/output limits, per-token prices, mode and capability flags, aliases. | `https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json` | MIT; BerriAI LiteLLM contributors | JSON | 14 days |
| `openrouter` | Optional cross-check: provider-specific context, modalities, per-token price, supported parameters, output cap, knowledge cutoff and dates. | `https://openrouter.ai/api/v1/models?output_modalities=all` | OpenRouter public API; OpenRouter | JSON | 7 days |
| `epoch` | Historical parameter counts and training compute. Enrichment only. | `https://epoch.ai/data/all_ai_models.csv` | CC BY 4.0; **cite Epoch AI** | CSV | 30 days |
| `huggingface` | Future source for open-weight architecture/parameter metadata. | `https://huggingface.co/api/models` | Variable by model; model authors | JSON | — |

`models.dev` is first in the merge order. `litellm` and `openrouter` fill fields
it omits. `epoch` never adds a model id; it only enriches a model that is
already present and whose normalized name matches plausibly.

The Hugging Face entry is registered but marked `role: future`. It is listed
for provenance and planning, appears as `not_assessed` in status output, and
`sync`/`import` reject it with `unsupported_source`. It is not fetched in this
slice.

Every source entry records its `id`, title, URL, fallback URLs, licence,
attribution, format, maximum bytes, staleness window, description, supplied
fields and role. `sources --json` exposes the whole registry.

## Cache layout and metadata

```
<state>/models/<source>.<json|csv>
<state>/models/<source>.meta.json
```

`<state>` is the normal ozagia state directory (`<root>/.ozagia`, or
`$OZAGIA_STATE_DIR`). Nothing is written outside it.

`<source>.meta.json` is small JSON with these required fields:

| Field | Meaning |
| --- | --- |
| `source` | Registry id. |
| `url` | URL that supplied the snapshot (the fallback URL when it was used). |
| `license` | Registry licence string. |
| `attribution` | Human attribution text. |
| `sha256` | SHA-256 of the snapshot bytes. |
| `bytes` | Snapshot size. |
| `fetched_at` | UTC import/fetch time, `YYYY-MM-DDTHH:MM:SSZ`. |
| `format` | `json` or `csv`. |
| `stale_after_s` | Registry staleness window in seconds. |
| `origin` | `import` or `network`. |
| `imported_from` | Local path, for an import only. |

Data and metadata are written with a temporary file and `os.replace`, so a
reader never sees a partially written file. The data file is a byte-for-byte
snapshot; it is not reformatted. The digest and byte count are verified on
every load.

Cache state is one of:

| Status | Meaning |
| --- | --- |
| `ok` | Digest valid, parsed, within the staleness window. |
| `stale` | Digest valid and parsed, but older than `stale_after_s`. Data is still used and the age is reported. |
| `missing` | No data and no metadata. Not fatal. |
| `malformed` | Metadata/data missing, unreadable, digest/size mismatch, invalid metadata or unparsable payload. Reported as `not_assessed`; no data is used. |
| `future` | Registered future source. Never fetched in this slice. |

Stale data is explicitly reported, never silently refreshed. A malformed cache
is not repaired automatically. An Epoch cache loads an enrichment index
(`CachedSource.epoch`, alias `epoch_index`) rather than a graph `external`
mapping; `merge_cached` applies it after the other sources.

## Commands

```text
ozagia-models sources [--root R] [--json]
ozagia-models sync SOURCE [--allow-network] [--timeout S] [--max-bytes N]
                        [--root R] [--json]
ozagia-models import SOURCE PATH [--root R] [--json]
```

`sync` without `--allow-network` prints a structured refusal and exits `2`; no
socket is opened. `--timeout` defaults to 60 seconds and is capped at 600.
`--max-bytes` defaults to the source's registry limit and is capped at 64 MiB.
Only `http`/`https` URLs and redirect targets are accepted, redirects are
capped at three and every hop is validated. Credentials in a URL are rejected;
no credential header is ever added.

`import` performs the same format/shape validation as a fetch and then writes
the cache. It is the supported path for an offline fixture, a mirrored
snapshot, or a snapshot reviewed by a person.

Examples:

```bash
# What is cached, how old, under which licence?
python -m ozagia.models_cli sources --root .

# One explicit network sync. Without --allow-network this exits 2.
python -m ozagia.models_cli sync models-dev --allow-network --root .

# Offline import of a reviewed snapshot.
python -m ozagia.models_cli import models-dev ./snapshots/models-dev.json \
  --root .

# Build the graph from all valid caches in precedence order.
python -m ozagia.models_cli graph --root . --json
```

`source_status(root)` returns the same registry/cache view as a dictionary:
`{"schema": 1, "sources": [...], "not_assessed": [...], "warnings": [...]}`.
Each entry includes the registry metadata plus `status`, `stale`, `age_s`,
`cache_path`, `meta_path`, `bytes`, `sha256`, `fetched_at`, `license`, `url`
and a `not_assessed` flag.

## Graph integration

`graph`, `similar`, `match` and `show` use cached sources by default in the
documented order `models-dev`, `litellm`, `openrouter`, `epoch`. The merged
mapping is passed as `model_graph.graph(..., external=...)` or
`model_graph.match(..., external=...)`.

```text
--source SOURCE     use one cached source (repeatable; precedence is still
                    registry order)
--no-sources        disable cached metadata and use curated profiles only
--with-metadata     show: print the cached metadata and provenance
```

The result JSON includes a `sources` object with the merge order, per-source
cache state, `not_assessed` ids and warnings. Text output prints a compact
`sources:` line and a `sources_not_assessed:` line when applicable.

The merge is field-by-field:

1. An explicit `[model_graph.models."provider/model"]` override wins for every
   key it sets.
2. Cached external metadata fills fields the curated profile omitted.
3. The curated built-in/repository profile remains for fields no cached source
   supplies.
4. Inferred values remain only where nothing else supplies the field.

A cached source never replaces a curated family/variant with an inferred one,
never invents a parameter count, and never changes `models.resolve`. External
metadata fills `context`, `input_cost`, `output_cost`, `modalities_*`,
`capabilities`, `params_total_b`/`params_active_b` (Epoch), `family`, `type`,
`source`, `confidence`, `notes` and `profile_source`. An explicit override
reports `source: "override"`; otherwise an externally annotated node reports
the source id (for example `models-dev`) and `confidence: "external"`.

### Precedence and staleness

- Merge order is fixed: `models-dev` > `litellm` > `openrouter` > `epoch`.
  `--source` selects a subset; it does not reorder precedence.
- Scalar and list fields are filled independently. The first source with a
  non-empty value wins; later sources fill absent/empty fields.
- Epoch runs after the other sources. It fills `params_total_b` only when the
  graph does not already know it, and appends a training-compute note when a
  conservative normalized-name match exists.
- A stale cache is still used and reported. A missing or malformed cache is
  reported in `not_assessed`; it does not fail the command.
- `--no-sources` leaves the curated profile/inferred behaviour unchanged.

## Field mapping

### models.dev

Root shapes accepted by the adapter:

1. `{ "providers": { "<provider>": { "models": { "<model>": {...} } } } }`
   optionally with a top-level canonical `models` map;
2. a single provider object `{ "id": ..., "models": {...} }`;
3. a provider map `{ "<provider>": { "models": {...} } }`;
4. a flat `{ "models": { "provider/model": {...} } }` map.

Canonical model data is merged under provider endpoint data. Endpoint
`limit`, `cost`, `modalities` and `capabilities` values win per key; canonical
data fills the rest. A provider's own `<provider>/<model>` id wins over a
canonical alias from another provider; aliases fill only absent ids. Primary
ids are built with the provider prefix even when the provider's model key
contains a slash.

| Source field | Graph/metadata field |
| --- | --- |
| `canonical_model_id` | `canonical_id` alias (only when it is a valid `provider/model` id). |
| `family` | `family`. |
| `open_weights` | `open_weights`; a note records open weights. |
| `release_date`, `knowledge` | `release_date`, `knowledge` (metadata only). |
| `limit.context`, `limit.output` | `context`/`input_limit`, `output_limit`. |
| `cost.input`, `cost.output` | USD per million tokens, unchanged. |
| `modalities.input/output` | `modalities_in`/`modalities_out`; `file` maps to `pdf`. |
| `capabilities.tools/reasoning/structured_output/computer_use` | matching graph capabilities; top-level `tool_call`, `reasoning`, `structured_output` flags are also read. |
| `attachment` | `vision` and image input. |
| `type` | Graph type; `decision` maps to `chat` with a note. |

Both `<provider>/<model>` and a supplied canonical id become aliases. No id is
invented from a display name.

### LiteLLM

| Source field | Graph/metadata field |
| --- | --- |
| map key | `<provider>/<key>`, or the key unchanged when it already contains a provider; provider is `litellm_provider`. |
| `max_input_tokens` | `context`. |
| `max_output_tokens`, `max_tokens` | `output_limit`. |
| `input_cost_per_token`, `output_cost_per_token` | multiplied by 1,000,000 to USD/M. |
| `mode` | Graph type (`chat`, `embedding`, `image*`, `audio*`). |
| `supports_function_calling`/`supports_tool_choice` | `tools`. |
| `supports_vision` | `vision` + image input. |
| `supports_pdf_input`/`supports_pdf` | `pdf_native` + PDF input. |
| `supports_audio_input`/`supports_audio_output` | `audio` + audio modality. |
| `supports_reasoning` | `reasoning`. |
| `supports_response_schema`/`supports_structured_output` | `structured_output`. |
| `supports_computer_use` | `computer_use`. |
| `supported_*_modalities` | input/output modalities; `file` maps to `pdf`. |
| `deprecation_date`, `source` | notes. |

### OpenRouter

| Source field | Graph/metadata field |
| --- | --- |
| `id` | Primary id (must already be `provider/model`). |
| `canonical_slug` | Alias when valid. |
| `context_length` | `context`. |
| `architecture.input_modalities/output_modalities` | modalities; `file` maps to `pdf`. |
| `pricing.prompt`, `pricing.completion` | per-token price × 1,000,000. Negative/unknown prices are omitted. |
| `supported_parameters` | `tools`, `structured_output`, `reasoning` (only those three are graph capabilities). |
| `top_provider.max_completion_tokens` | `output_limit`. |
| `knowledge_cutoff` | `knowledge`. |
| `created` | `release_date`. |
| `expiration_date` | note. |

OpenRouter prices are the listed route, not necessarily the first-party price.

### Epoch CSV

The adapter reads these columns case-insensitively:

`Model`, `Organization`, `Publication date`, `Parameters`,
`Training compute (FLOP)`, `Open model weights?`, `Base model`,
`Hugging Face developer id`, `Domain`, `Task`.

Parameter cells are parsed instead of guessed:

| Input | Billions |
| --- | --- |
| `1.8e12` | 1800 |
| `175B` | 175 |
| `2.4T` | 2400 |
| `70` | 70 (the column's billions convention) |
| `1,800,000,000` | 1.8 |
| `unknown` | `null` |

Normalized aliases are built from the model name, organization/model,
base-model name and Hugging Face developer/model. Common qualifiers such as
`instruct`, `chat`, `base`, `preview` and `latest` are stripped only from the
end of a name, and only when the remaining key is still at least four
characters. A full `provider/model` match is preferred; an ambiguous model
name (two different rows) yields no match. `apply_epoch(external, epoch)`
returns a new mapping and never mutates its argument. `merge_cached(...,
model_ids=...)` seeds catalog ids for Epoch, so a model known only to the local
catalog can still receive a plausible parameter count; ids outside
`model_ids` are never added.

## Limitations

- models.dev normally has **no parameter counts**. Closed models stay
  `params_total_b: null` unless Epoch (or a later Hugging Face adapter)
  supplies a plausible match.
- Epoch is a historical research table, not a current provider catalogue.
  Names change; matching is conservative and deliberately leaves models
  unenriched rather than guessing.
- OpenRouter prices are route-specific. They are an optional cross-check, not
  a universal first-party price.
- LiteLLM names include provider aliases and historical entries. The adapter
  skips entries with no derivable provider instead of inventing one.
- Caches are explicit. There is no automatic sync, background refresh, ETag
  handling, credential, API key or signature verification beyond HTTPS,
  redirect validation, byte caps, JSON/CSV validation and SHA-256 of the
  stored snapshot.
- The Hugging Face source is future work and is not fetched or imported.
- The graph remains advisory. Cached metadata does not change
  `models.resolve`, route a request or select a model automatically.
- Cache metadata is not a signed manifest; a person who can write the state
  directory can replace a snapshot and its digest. `import` is the supported
  path for a reviewed offline snapshot.

## Tests

```bash
PYTHONPATH=src python -m pytest -q \
  tests/test_model_sources.py tests/test_model_graph.py tests/test_models.py
ruff check src tests
```

The tests use checked-in fixtures for the models.dev API/catalog/models shapes,
LiteLLM, OpenRouter and Epoch CSV. Network tests inject a fake opener and cover
refusal, byte caps (declared and streamed), content type, timeout, redirect
scheme, fallback, sha256 metadata and atomic writes. No test contacts a
provider, runs `opencode` or calls a model.
