# Rules Engine

*One YAML format to rule them all.*

Rules are the unified framework behind hints, checks, library suggestions,
API spec fetching, anti-pattern detection, complexity scoring, brevity
constraints, and embedded language detection. One YAML format, one regex
engine, pluggable actions.

## Three layers, one engine

| Layer | Stage | What | Action |
|---|---|---|---|
| **Hints** | input | Inject guidance before generation | `inject` |
| **Checks** | output | Validate model output | `resubmit`, `warn`, `block`, `fix`, `run` |
| **Rules** | both | Everything else | any action |

All three use the same YAML format and regex engine. Use whichever API
matches your use case:

```python
# Hints (input-side guidance) — 259 built-in
from ozmalib.hints import load_hints, check_hints

# Checks (output-side validation) — 47 built-in
from ozmalib.checks import load_checks, run_checks

# Rules (unified) — 64 built-in
from ozmalib.rules import load_rules, match_rules
```

## Actions

| Action | Stage | What it does |
|---|---|---|
| `inject` | input | Inject text into model context |
| `suggest` | input | Suggest a library/tool (non-blocking) |
| `fetch_spec` | input | Fetch an API spec and inject it |
| `score` | input | Contribute to a named numeric score read by downstream consumers |
| `resubmit` | output | Send corrective feedback to model |
| `warn` | output | Log a finding, continue |
| `block` | output | Reject output entirely |
| `fix` | output | Run a command to auto-fix (e.g., formatter) |
| `run` | output | Run a command for analysis (e.g., linter) |

### `score` action — specification

`score` differs from the other actions in that it produces a numeric
contribution rather than a side effect on context or output. It
requires a typed payload:

```yaml
name: score-algo-complexity
action: score
payload:
  score_field: algorithmic_complexity   # named bucket; any identifier
  points: 3                             # additive contribution (int or float, may be negative)
  cap: 10                               # optional per-rule cap on cumulative contribution
```

**Evaluation semantics:**

- Multiple `score` rules matching the same request accumulate into
  the named `score_field`. Totals are computed at rule-evaluation
  end, per field.
- Downstream consumers (complexity gate, model-routing classifier,
  budget enforcer) read scored fields by name. Consumers are not
  part of the rule schema; they are pipeline components that declare
  which score fields they read.
- Fields not read by any consumer are computed but discarded.
- Score values are bounded-float; per-rule `cap` limits a single
  rule's contribution; per-field-per-request caps may be enforced
  by the consumer.
- Telemetry records the final score per field plus top-contributing
  rules for debugging.

**Difference from other actions.** Other actions produce side effects
(inject text, emit suggestions, fix code). `score` produces a value
the pipeline reads. Think of it as the one "pure functional" action —
its output is a number, not an operation.

## YAML format

```yaml
name: suggest-httpx
stage: input              # "input", "output", or "both"
type: library             # category for filtering
action: suggest           # what to do when matched
priority: 40              # 1-100 (higher = more important)
severity: info            # critical, high, medium, low, info

detect:                   # ANY match fires the rule
  - '\bhttp client\b'
  - '\bfetch url\b'
  - '\bapi call\b'

guard:                    # ANY match suppresses the rule
  - '\b(?:explain|what is)\b'

detect_code:              # patterns matched against file_context only
  - 'requests\.get\('

match_all: false          # true = ALL detect patterns must match

payload:                  # action-specific data
  library: httpx
  import: "import httpx"
  example: 'resp = httpx.get("https://..."); data = resp.json()'
  text: "Use httpx for HTTP requests."
  command: "ruff format {file}"    # for fix/run actions
```

### Legacy compatibility

Rules also accept `guidance`, `message`, and `command` as top-level fields
(they map to `payload.text` and `payload.command`). This means existing
hint and check YAML files work as rules without modification.

### Payload schema — per action

Each action has a typed payload schema enforced at rule load time
(not at match time). Rules with invalid payloads are rejected with
`payload_schema_invalid` logged and not installed.

| Action | Required payload fields | Optional payload fields |
|---|---|---|
| `inject` | `text` (string) | `importance`, `scope` |
| `suggest` | `library` (string) | `import`, `example`, `text` |
| `fetch_spec` | `spec_url` (URL) or `spec_ref` (well-known name) | `cache_ttl` |
| `score` | `score_field` (string), `points` (number) | `cap` (number) |
| `resubmit` | `feedback` (string) | `max_retries` |
| `warn` | `message` (string) | `severity` |
| `block` | `reason` (string) | — |
| `fix` / `run` | `command` (string) | `cwd`, `timeout` |

The complete JSON schema is generated from this table and lives at
`ozmalib/rules/schema/payload.json`. CI validates every built-in rule
against the schema. Implementations MUST reject rules whose payload
violates the schema for the declared action.

## System totals

| Engine | Files | Stage |
|---|---|---|
| Hints | 259 | input |
| Checks | 47 | output |
| Rules | 64 | both |
| **Total** | **370** | |

## Built-in rules (64)

### Library suggestions (25)

Detect task descriptions mentioning common problems and suggest the right
library. Input-side, `action: suggest`.

| Rule | Detects | Suggests |
|---|---|---|
| `suggest-httpx` | http client, fetch url, api call | httpx |
| `suggest-fastapi` | rest api, web server, endpoint | fastapi |
| `suggest-polars` | csv, dataframe, tabular | polars |
| `suggest-pydantic` | validate, schema, data model | pydantic |
| `suggest-typer` | cli, command line, argparse | typer |
| `suggest-pytest` | unit test, test case, mock | pytest |
| `suggest-sqlalchemy` | orm, database model, migration | sqlalchemy |
| `suggest-websockets` | websocket, real-time | websockets |
| `suggest-duckdb` | analytical query, olap | duckdb |
| `suggest-orjson` | json serialization, fast json | orjson |
| `suggest-pyyaml` | yaml, config file | pyyaml |
| `suggest-msgspec` | binary serialization, msgpack | msgspec |
| `suggest-rich` | terminal ui, progress bar | rich |
| `suggest-hypothesis` | property-based testing, fuzzing | hypothesis |
| `suggest-pendulum` | datetime, timezone | pendulum |
| `suggest-anyio` | async, concurrency | anyio |
| `suggest-tenacity` | retry, backoff | tenacity |
| `suggest-redis` | cache, pub/sub, message queue | redis |
| `suggest-structlog` | structured logging | structlog |
| `suggest-watchdog` | file watcher, inotify | watchdog |
| `suggest-jinja2` | template, html rendering | jinja2 |
| `suggest-pillow` | image processing, resize | pillow |
| `suggest-pypdf` | pdf, document parsing | pypdf |
| `suggest-pyjwt` | jwt, token, authentication | pyjwt |
| `suggest-bcrypt` | password hashing, bcrypt | bcrypt |

### Anti-patterns (15)

Detect deprecated/suboptimal libraries in generated code and suggest
replacements. Output-side, `action: resubmit`.

| Rule | Detects | Suggests |
|---|---|---|
| `antipattern-flask` | `from flask import` | fastapi |
| `antipattern-django` | `from django` | fastapi |
| `antipattern-requests` | `import requests` | httpx |
| `antipattern-pandas` | `import pandas` | polars |
| `antipattern-argparse` | `import argparse` | typer |
| `antipattern-os-path` | `os.path.join()` | pathlib.Path |
| `antipattern-json-dumps` | `json.dumps()` in hot path | orjson |
| `antipattern-unittest` | `class X(TestCase)` | pytest |
| `antipattern-sqlalchemy-query` | legacy Query API | select() style |
| `antipattern-pydantic-v1-validator` | `@validator` | `@field_validator` |
| `antipattern-pydantic-v1-dict` | `.dict()` | `.model_dump()` |
| `antipattern-aiohttp` | `import aiohttp` | httpx |
| `antipattern-asyncio-gather` | bare `asyncio.gather` | `TaskGroup` |
| `antipattern-print-debug` | `print()` debugging | structlog |
| `antipattern-logging-basic` | `logging.basicConfig` | structlog |

### API spec fetching (16)

Detect API integration requests and provide spec URLs for auto-fetching.
Input-side, `action: fetch_spec`.

Stripe, PayPal, Twilio, SendGrid, GitHub, GitLab, Cloudflare, Auth0,
Elasticsearch, OpenAI, Shopify, Slack, Discord, Datadog, PagerDuty, HubSpot.

### Complexity scoring (3)

Detect algorithm, system, and structural complexity keywords. Input-side,
`action: score`.

### Brevity constraints (3)

Model-tier-aware brevity constraints. Input-side, `action: inject`.
Triggered by synthetic markers (`__model_tier_small__`, etc.).

### Embedded language detection (2)

Detect SQL and Jinja2 in code for compression routing. Input-side.

## Action helpers

```python
from ozmalib.rules import (
    load_rules, match_rules,
    collect_guidance,           # inject + suggest → text block
    collect_resubmit_feedback,  # resubmit → corrective prompt
    get_library_suggestions,    # suggest → structured list
    render_library_suggestions, # suggest → prompt block
    get_spec_urls,              # fetch_spec → URL list
    get_antipattern_feedback,   # antipattern → structured list
    compute_complexity_score,   # score → (points, level)
    has_blocking,               # any block action?
    run_fix_commands,           # fix/run → execute commands
)

rules = load_rules()

# Input-side processing
input_results = match_rules(user_message, rules, stage="input")
guidance = collect_guidance(input_results)
suggestions = get_library_suggestions(input_results)
spec_urls = get_spec_urls(input_results)

# Output-side processing
output_results = match_rules(model_output, rules, stage="output")
if has_blocking(output_results):
    raise ValueError("Output blocked")
feedback = collect_resubmit_feedback(output_results)
code = run_fix_commands(output_results, model_output)
```

## Extending

### Add a new rule

Drop a YAML file in `ozma/rules/data/` or any directory passed to
`load_rules()`. The rule is immediately available — no registration,
no code changes.

### Add a new action

1. Define the action name in `VALID_ACTIONS` in `engine.py`
2. Add a handler in `actions.py`
3. Write YAML rules that use it

### Override built-in rules

Later directories override earlier ones by `name`:

```python
rules = load_rules("/my-project/rules")
# Rules in /my-project/rules/ override built-in rules with the same name
```

### Project-specific rules

Drop YAML files in `.rules/` at your repo root (or `.hints/` for hints,
which also works with the rules engine via legacy compat).
